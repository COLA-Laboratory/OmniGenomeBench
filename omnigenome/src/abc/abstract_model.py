# -*- coding: utf-8 -*-
# file: omnigenome_model.py
# time: 18:36 06/04/2024
# author: YANG, HENG <hy345@exeter.ac.uk> (杨恒)
# github: https://github.com/yangheng95
# huggingface: https://huggingface.co/yangheng
# google scholar: https://scholar.google.com/citations?user=NPq5a_0AAAAJ&hl=en
# Copyright (C) 2019-2024. All Rights Reserved.
import json
import os
import shutil
import warnings
import inspect
import findfile
import torch
from transformers import AutoModel, AutoConfig, AutoTokenizer, BatchEncoding

from ..misc.utils import RNA2StructureCache
from ..misc.utils import fprint, env_meta_info
from ...src.model.module_utils import InteractingAttention


def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


class OmniGenomeModel(torch.nn.Module):
    def __init__(self, config_or_model_model, tokenizer, *args, **kwargs):
        self.loss_fn = None

        label2id = kwargs.pop("label2id", None)
        trust_remote_code = kwargs.pop("trust_remote_code", True)
        num_labels = kwargs.pop("num_labels", None)
        ignore_mismatched_sizes = kwargs.pop("ignore_mismatched_sizes", False)

        if label2id is not None and num_labels is None:
            num_labels = len(label2id)

        # do not change the order of the following lines
        super().__init__(*args, **kwargs)

        if isinstance(config_or_model_model, str):
            config = AutoConfig.from_pretrained(
                config_or_model_model,
                num_labels=num_labels,
                label2id=label2id,
                trust_remote_code=trust_remote_code,
            )
            self.model = AutoModel.from_pretrained(
                config_or_model_model,
                config=config,
                trust_remote_code=trust_remote_code,
                ignore_mismatched_sizes=ignore_mismatched_sizes,
            )
            self.model.config = config
        elif isinstance(config_or_model_model, torch.nn.Module):
            self.model = config_or_model_model
            self.model.config.num_labels = num_labels
            self.model.config.label2id = label2id
        elif isinstance(config_or_model_model, AutoConfig):
            config = config_or_model_model
            config.num_labels = num_labels
            config.label2id = label2id
            self.model = AutoModel.from_config(config)
            self.model.config = config
        else:
            raise ValueError(
                "The config_or_model_model should be either a string, a torch.nn.Module or a AutoConfig object."
            )

        # Update the config
        self.config = self.model.config
        if isinstance(label2id, dict):
            self.config.label2id = label2id
            self.config.id2label = {v: k for k, v in label2id.items()}

        # The metadata of the model
        self.metadata = env_meta_info()
        self.metadata["model_cls"] = self.__class__.__name__

        # The config of the model
        if hasattr(self.config, "n_embd"):
            self.config.hidden_size = self.config.n_embd
        elif hasattr(self.config, "d_model"):
            self.config.hidden_size = self.config.d_model
        elif hasattr(self.config, "hidden_size"):
            self.config.hidden_size = self.config.hidden_size
        else:
            raise RuntimeError(
                "The hidden size of the model is not found in the config."
            )

        # The tokenizer of the model
        self.tokenizer = tokenizer
        self.metadata["tokenizer_cls"] = self.tokenizer.__class__.__name__
        if hasattr(self.tokenizer, "base_tokenizer"):
            self.pad_token_id = self.tokenizer.base_tokenizer.pad_token_id
        else:
            self.pad_token_id = self.tokenizer.pad_token_id

        self.dropout = torch.nn.Dropout(kwargs.get("dropout", 0.0))
        self.activation = torch.nn.Tanh()

    def last_hidden_state_forward(self, inputs):
        """
        :param inputs: The inputs to the model
        :return: The last hidden state of the model and the secondary structure information if ss is not None
        """
        model = self.model
        input_mapping = {}

        # Determine the input parameter names of the model's forward method
        forward_params = inspect.signature(model.forward).parameters

        if isinstance(inputs, tuple):
            input_ids = inputs[0]
            attention_mask = inputs[1] if len(inputs) > 1 else None
        elif isinstance(inputs, BatchEncoding) or isinstance(inputs, dict):
            input_ids = inputs.get("input_ids", None)
            attention_mask = inputs.get("attention_mask", None)
        elif isinstance(inputs, torch.Tensor):
            shape = inputs.shape
            try:
                if len(shape) == 3:
                    if shape[1] == 2:
                        input_ids = inputs[:, 0]
                        attention_mask = inputs[:, 1]
                    else:
                        input_ids = inputs[0]
                        attention_mask = inputs[1] if len(inputs) > 1 else None
                elif len(shape) == 2:
                    input_ids = inputs
                    attention_mask = None
                else:
                    raise ValueError(
                        f"Failed to get the input_ids and attention_mask from the inputs, got shape {shape}.")
            except:
                raise ValueError(f"Failed to get the input_ids and attention_mask from the inputs, got shape {shape}.")
        else:
            raise ValueError(
                f"The inputs should be a tuple, BatchEncoding or a dictionary-like object, got {type(inputs)}.")

        # Map the inputs to the forward method parameters
        for param in forward_params:
            if param in inputs:
                input_mapping[param] = inputs[param]

        try:
            if "2DStructure" in self.metadata["model_name"]:
                outputs = self._structure_hidden_state_forward(inputs)
            else:
                outputs = model(**input_mapping, output_hidden_states=True)
        except Exception as e:
            try:
                outputs = model(input_ids=input_ids, output_hidden_states=True)
            except Exception as e:
                if 'x' in forward_params:
                    try:
                        outputs = model(x=input_ids)
                    except Exception as e:
                        raise RuntimeError(f"Failed to get the last hidden state from the model, got error: {e}")
                else:
                    raise ValueError("The model does not accept 'x' as input.")

        if not hasattr(outputs, "last_hidden_state"):
            warnings.warn(f"last_hidden_state not found in the outputs from the {model.__class__.__name__} model.")

        if hasattr(outputs, "last_hidden_state"):
            last_hidden_state = outputs.last_hidden_state
        elif isinstance(outputs, dict) and "last_hidden_state" in outputs:
            last_hidden_state = outputs["last_hidden_state"]
        elif hasattr(outputs, "hidden_states"):
            last_hidden_state = outputs.hidden_states[-1]
        elif isinstance(outputs, (list, tuple, torch.Tensor)):
            last_hidden_state = outputs[-1] if len(outputs[-1].shape) == 3 else outputs[0]
        else:
            raise ValueError(
                f"Cannot find the last hidden state in the outputs from the {model.__class__.__name__} model, please check the model architecture.")

        return last_hidden_state

    # def last_hidden_state_forward(self, inputs):
    #     """
    #
    #     :param inputs: The inputs to the model
    #     :return: The last hidden state of the model and the secondary structure information if ss is not None
    #     """
    #     model = self.model
    #     if isinstance(inputs, tuple):
    #         input_ids = inputs[0]
    #         attention_mask = inputs[1] if len(inputs) > 1 else None
    #     elif isinstance(inputs, BatchEncoding) or isinstance(inputs, dict):
    #         input_ids = inputs["input_ids"]
    #         attention_mask = (
    #             inputs["attention_mask"] if "attention_mask" in inputs else None
    #         )
    #     elif isinstance(inputs, torch.Tensor):
    #         shape = inputs.shape
    #         try:
    #             if len(shape) == 3:
    #                 # compatible with hf_trainer in AutoBenchmark
    #                 if shape[1] == 2:
    #                     input_ids = inputs[:, 0]
    #                     attention_mask = inputs[:, 1]
    #                 else:
    #                     input_ids = inputs[0]
    #                     attention_mask = inputs[1] if len(inputs) > 1 else None
    #             elif len(shape) == 2:
    #                 input_ids = inputs
    #                 attention_mask = None
    #             else:
    #                 raise ValueError(
    #                     f"Failed to get the input_ids and attention_mask from the inputs, got shape {shape}."
    #                 )
    #         except:
    #             raise ValueError(
    #                 f"Failed to get the input_ids and attention_mask from the inputs, got shape {shape}."
    #             )
    #     else:
    #         raise ValueError(
    #             f"The inputs should be a tuple, BatchEncoding or a dictionary-like object, got {type(inputs)}."
    #         )
    #
    #     try:
    #         if "2DStructure" in self.metadata["model_name"]:
    #             outputs = self._structure_hidden_state_forward(inputs)
    #
    #         else:
    #             outputs = model(
    #                 input_ids,
    #                 attention_mask=attention_mask,
    #                 output_hidden_states=True,
    #             )
    #
    #     except Exception as e:
    #         try:
    #             # For autoregressive models, the attention_mask is not required
    #             outputs = model(
    #                 input_ids,
    #                 output_hidden_states=True,
    #             )
    #
    #         except Exception as e:
    #             try:
    #                 outputs = model(x=input_ids)
    #             except:
    #                 raise RuntimeError(
    #                     f"Failed to get the last hidden state from the model, got error: {e}"
    #                 )
    #
    #     if not hasattr(outputs, "last_hidden_state"):
    #         warnings.warn(
    #             f"last_hidden_state not found in the outputs from the {model.__class__.__name__} model."
    #         )
    #
    #     if hasattr(outputs, "last_hidden_state"):
    #         last_hidden_state = outputs.last_hidden_state
    #     elif isinstance(outputs, dict) and "last_hidden_state" in outputs:
    #         last_hidden_state = outputs["last_hidden_state"]
    #     elif hasattr(outputs, "hidden_states"):
    #         last_hidden_state = outputs.hidden_states[-1]
    #     elif (
    #         isinstance(outputs, list)
    #         or isinstance(outputs, tuple)
    #         or isinstance(outputs, torch.Tensor)
    #     ):
    #         # For some models like DNABERT-2, the outputs is a list, tuple of tensors
    #         last_hidden_state = (
    #             outputs[-1] if len(outputs[-1].shape) == 3 else outputs[0]
    #         )
    #     else:
    #         raise ValueError(
    #             f"Cannot find the last hidden state in the outputs from the {model.__class__.__name__} \
    #             model, please check the model architecture."
    #         )
    #
    #     return last_hidden_state

    def loss_function(self, logits, labels):
        raise NotImplementedError(
            "The loss_function() function should be implemented for your model."
        )

    def set_loss_fn(self, loss_function):
        self.loss_fn = loss_function

    def predict(self, sequence_or_inputs, **kwargs):
        # Please implement the predict() function for your model
        raw_outputs = self._forward_from_raw_input(sequence_or_inputs, **kwargs)
        return raw_outputs

    def inference(self, sequence_or_inputs, **kwargs):
        # Please implement the predict() function for your model
        raw_outputs = self._forward_from_raw_input(sequence_or_inputs, **kwargs)
        return raw_outputs

    def _structure_hidden_state_forward(self, inputs):
        model = self.model

        seq_outputs = model(
            inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            output_hidden_states=True,
        )

        if not hasattr(model, "rna2structure"):
            model.rna2structure = RNA2StructureCache()

        # if not hasattr(model, "layer_norm"):
        #     model.layer_norm = torch.nn.LayerNorm(
        #         seq_outputs.last_hidden_state.shape[1:]
        #     )
        #     model.layer_norm.to(seq_outputs.last_hidden_state.device)
        if not hasattr(model, "attn_head"):
            model.attn_head = torch.nn.Linear(
                seq_outputs.last_hidden_state.shape[-2], 1
            )
            model.attn_head.to(seq_outputs.last_hidden_state.device)

        tokenizer = AutoTokenizer.from_pretrained(self.config.name_or_path)
        input_ids = inputs["input_ids"]
        sequences = tokenizer.batch_decode(input_ids, skip_special_tokens=True)
        sequences = [seq.replace(" ", "") for seq in sequences]
        structures = model.rna2structure.fold([seq for seq in sequences])

        # structures = [
        #     f"{sequence}{tokenizer.eos_token}{structure}"
        #     for (sequence, structure) in zip(sequences, structures)
        # ]

        tokenized_struct = tokenizer(
            structures,
            padding="max_length",
            max_length=input_ids.shape[1],
            truncation=True,
            return_tensors="pt",
            add_special_tokens=True,
        )
        tokenized_struct.to(input_ids.device)
        str_outputs = model(
            **tokenized_struct,
            output_hidden_states=True,
        )
        last_hidden_state = seq_outputs.last_hidden_state
        ss_last_hidden_state = str_outputs.last_hidden_state

        # inter_hidden_state = torch.matmul(last_hidden_state, ss_last_hidden_state.transpose(-1, -2))
        # attn_logits = model.attn_head(inter_hidden_state)
        # mix_last_hidden_state = torch.mul(attn_logits, ss_last_hidden_state)
        # mix_last_hidden_state = model.layer_norm(mix_last_hidden_state)
        # mix_last_hidden_state = torch.matmul(inter_hidden_state, last_hidden_state)

        mix_last_hidden_state = torch.mul(last_hidden_state, ss_last_hidden_state)
        # last_hidden_state = inter_hidden_state + last_hidden_state

        seq_outputs.last_hidden_state = mix_last_hidden_state
        return seq_outputs

    def __call__(self, inputs, labels=None, *args, **kwargs):
        if isinstance(inputs, dict):
            labels = inputs.get("labels", None)
            label = inputs.get("label", None)
            labels = labels if labels is not None else label
            # if labels is None:
            #     warnings.warn(
            #         "No labels are provided in the inputs, the model will not calculate the loss.",
            #     )
        elif isinstance(inputs, tuple):
            labels = inputs[1]
            inputs = inputs[0]
        elif labels is not None:
            labels = labels

        outputs = self.forward(inputs, labels=labels)

        if labels is not None:
            outputs["loss"] = self._calculate_loss(outputs, labels)
        else:
            outputs["loss"] = None
        return outputs

    def _calculate_loss(self, outputs, labels):
        loss = outputs.get("loss", None)
        if loss is not None:
            return outputs

        logits = outputs["logits"]
        if logits is not None or labels is not None:
            loss = self.loss_function(logits, labels)
            return loss
        else:
            raise RuntimeError(
                "The output of the forward() function should be a dictionary-like objective"
                " and have either 'loss', or 'logits' and 'labels' attribute."
            )

    def save(self, path, overwrite=False, dtype=torch.float16, **kwargs):
        self.eval()

        if os.path.exists(path) and not overwrite:
            raise FileExistsError(
                f"The path {path} already exists, please set overwrite=True to overwrite it."
            )

        if not os.path.exists(path):
            os.makedirs(path)

        for file in findfile.find_files(
            self.config.name_or_path,
            and_key=[],
            exclude_key=["pytorch_model", "model", "safetensors"],
        ):
            shutil.copyfile(file, f"{path}/{os.path.basename(file)}")

        _device = self.model.device
        _dtype = self.model.dtype
        self.model.to(dtype).to("cpu")
        self.tokenizer.save_pretrained(path)

        with open(f"{path}/metadata.json", "w", encoding="utf8") as f:
            json.dump(self.metadata, f)
        self.model.save_pretrained(
            f"{path}", safe_serialization=False
        )  # do not remove this line, used to save customed model scripts
        with open(f"{path}/pytorch_model.bin", "wb") as f:
            torch.save(self.state_dict(), f)

        self.model.to(_dtype).to(_device)
        fprint(f"The model is saved to {path}.")

    def load(self, path, **kwargs):
        with open(f"{path}/metadata.json", "r", encoding="utf8") as f:
            metadata = json.load(f)

        if metadata["model_cls"] != self.__class__.__name__:  # Check the model class
            raise ValueError(
                f"The model class in the loaded model is {metadata['model_cls']}, "
                f"but the current model class is {self.__class__.__name__}."
            )
        config = AutoConfig.from_pretrained(path, trust_remote_code=True, **kwargs)

        for key, value in config.__dict__.items():
            if key not in self.config.__dict__ or self.config.__dict__[key] != value:
                fprint(
                    f"Warning: The value of the key {key} in the loaded model is {value}, "
                    f"but the current value is {self.config.__dict__.get(key, None)}."
                )

        with open(f"{path}/pytorch_model.bin", "rb") as f:
            self.load_state_dict(
                torch.load(f, map_location=kwargs.get("device", "cpu")), strict=True
            )
        return self

    def _forward_from_raw_input(self, sequence_or_inputs, **kwargs):
        if not isinstance(sequence_or_inputs, BatchEncoding) and not isinstance(
            sequence_or_inputs, dict
        ):
            inputs = self.tokenizer(
                sequence_or_inputs,
                padding=kwargs.pop("padding", True),
                max_length=kwargs.pop("max_length", 1024),
                truncation=kwargs.pop("truncation", True),
                return_tensors=kwargs.pop("return_tensors", "pt"),
                **kwargs,
            )
        else:
            inputs = sequence_or_inputs
        inputs = inputs.to(self.model.device)
        for col in inputs:
            if inputs[col] is not None and inputs[col].dtype == torch.int64:
                inputs[col] = inputs[col].to(torch.int32)
        with torch.no_grad():
            raw_outputs = self(inputs)
            raw_outputs["inputs"] = inputs
        return raw_outputs

    @staticmethod
    def from_pretrained(model_name_or_path, tokenizer, *args, **kwargs):
        config = kwargs.pop("config", None)
        if config is None:
            config = AutoConfig.from_pretrained(model_name_or_path, **kwargs)
        base_model = AutoModel.from_pretrained(model_name_or_path, **kwargs)
        if tokenizer is None:
            tokenizer = AutoTokenizer.from_pretrained(base_model, **kwargs)
        return OmniGenomeModel(config, base_model, tokenizer, *args, **kwargs)

    def model_info(self):
        info = f"Model Name: {self.__class__.__name__}\n"
        info += f"Model Metadata: {self.metadata}\n"
        info += f"Base Model Name: {self.config.name_or_path}\n"
        info += f"Model Type: {self.config.model_type}\n"
        info += f"Model Architecture: {self.config.architectures}\n"
        info += f"Model Parameters: {count_parameters(self.model) / 1e6} M\n"
        info += f"Model Config: {self.config}\n"
        fprint(info)
        return info
