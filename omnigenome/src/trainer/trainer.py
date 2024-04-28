# -*- coding: utf-8 -*-
# file: trainer.py
# time: 14:40 06/04/2024
# author: YANG, HENG <hy345@exeter.ac.uk> (杨恒)
# github: https://github.com/yangheng95
# huggingface: https://huggingface.co/yangheng
# google scholar: https://scholar.google.com/citations?user=NPq5a_0AAAAJ&hl=en
# Copyright (C) 2019-2024. All Rights Reserved.
import os

import numpy as np
import torch
from tqdm import tqdm
import autocuda

from ..misc.utils import env_meta_info, fprint


class Trainer:
    def __init__(
            self,
            model,
            train_loader: torch.utils.data.DataLoader = None,
            eval_loader: torch.utils.data.DataLoader = None,
            test_loader: torch.utils.data.DataLoader = None,
            epochs: int = 3,
            patience: int = 3,
            optimizer: torch.optim.Optimizer = None,
            loss_fn: torch.nn.Module = None,
            compute_metrics: [list, str] = None,
            seed: int = 42,
            device: [torch.device, str] = None,
            *args,
            **kwargs,
    ):
        self.model = model
        self.train_loader = train_loader
        self.eval_loader = eval_loader
        self.test_loader = test_loader
        self.epochs = epochs
        self.patience = patience
        self.optimizer = optimizer
        self.loss_fn = loss_fn
        self.compute_metrics = (
            compute_metrics if isinstance(compute_metrics, list) else [compute_metrics]
        )
        self.seed = seed
        self.device = device if device else autocuda.auto_cuda()
        if self.loss_fn is not None:
            self.model.set_loss_fn(self.loss_fn)
        self.model.to(self.device)

        self.metadata = env_meta_info()
        self.metrics = {}

        self.trial_name = kwargs.get("trial_name", self.model.__class__.__name__)

        self._save_state_dict()

    def _is_metric_better(self, metrics, stage="valid"):

        assert stage in ["valid", "test"], "The metrics stage should be either 'valid' or 'test'."

        fprint(metrics)

        prev_metrics = self.metrics.get(stage, None)

        if not prev_metrics:
            if stage not in self.metrics:
                self.metrics.update({f"{stage}": [metrics]})
            else:
                self.metrics[f"{stage}"].append(metrics)
            return True

        is_prev_increasing = np.mean(list(prev_metrics[0].values())) <= np.mean(list(prev_metrics[-1].values()))
        is_still_increasing = np.mean(list(prev_metrics[-1].values())) <= np.mean(list(metrics.values()))

        is_prev_decreasing = np.mean(list(prev_metrics[0].values())) >= np.mean(list(prev_metrics[-1].values()))
        is_still_decreasing = np.mean(list(prev_metrics[-1].values())) >= np.mean(list(metrics.values()))

        if stage not in self.metrics:
            self.metrics.update({f"{stage}": [metrics]})
        else:
            self.metrics[f"{stage}"].append(metrics)

        if is_prev_increasing and is_still_increasing:
            return True
        elif is_prev_decreasing and is_still_decreasing:
            return True
        else:
            return False

    def train(self, path_to_save=None, autocast=True, **kwargs):
        patience = 0
        for epoch in range(self.epochs):
            self.model.train()
            train_loss = []
            train_it = tqdm(
                self.train_loader, desc=f"Epoch {epoch + 1}/{self.epochs} Loss:"
            )
            for batch in train_it:
                batch.to(self.device)
                if autocast:
                    with torch.cuda.amp.autocast():
                        loss = self.model(batch)["loss"]
                else:
                    loss = self.model(batch)["loss"]
                loss.backward()
                self.optimizer.step()
                self.optimizer.zero_grad()
                train_loss.append(loss.item())
                train_it.set_description(
                    f"Epoch {epoch + 1}/{self.epochs} Loss: {np.average(train_loss):.4f}"
                )

            if self.eval_loader is not None and len(self.eval_loader) > 0:
                valid_metrics = self.evaluate()
            else:
                valid_metrics = self.test()
            if self._is_metric_better(valid_metrics, stage="valid"):
                self._save_state_dict()
                patience = 0
            else:
                patience += 1
                if patience >= self.patience:
                    fprint(f"Early stopping at epoch {epoch}.")
                    break

            if path_to_save:
                _path_to_save = path_to_save + "_epoch_" + str(epoch)

                if valid_metrics:
                    for key, value in valid_metrics.items():
                        _path_to_save += f"_seed_{self.seed}_{key}_{value:.4f}"

                self.save_model(path_to_save, **kwargs)

        if self.test_loader is not None and len(self.test_loader) > 0:
            self._load_state_dict()
            test_metrics = self.test()
            self._is_metric_better(test_metrics, stage="test")

        if path_to_save:
            _path_to_save = path_to_save + "_final"
            if self.metrics['test_metrics']:
                for key, value in self.metrics['test_metrics'][-1].items():
                    _path_to_save += f"_seed_{self.seed}_{key}_{value:.4f}"

            self.save_model(path_to_save, **kwargs)

        return self.metrics

    def evaluate(self):
        valid_metrics = {}
        with torch.no_grad():
            self.model.eval()
            val_truth = []
            val_preds = []
            it = tqdm(self.eval_loader, desc="Evaluating")
            for batch in it:
                batch.to(self.device)
                predictions = self.model.predict(batch)["predictions"]
                val_truth.append(batch["labels"].detach().cpu().numpy())
                val_preds.append(np.array(predictions))

            val_truth = np.concatenate(val_truth)
            val_preds = np.concatenate(val_preds)
            for metric_func in self.compute_metrics:
                valid_metrics.update(metric_func(val_truth, val_preds))
            return valid_metrics

    def test(self):
        test_metrics = {}
        with torch.no_grad():
            self.model.eval()
            preds = []
            truth = []
            it = tqdm(self.test_loader, desc="Testing")
            for batch in it:
                batch.to(self.device)
                predictions = self.model.predict(batch)["predictions"]
                truth.append(batch["labels"].detach().cpu().numpy())
                preds.append(predictions)
            preds = np.concatenate(preds)
            truth = np.concatenate(truth)
            for metric_func in self.compute_metrics:
                test_metrics.update(metric_func(truth, preds))
            return test_metrics

    def predict(self, data_loader):
        return self.model.predict(data_loader)

    def get_model(self, **kwargs):
        return self.model

    def compute_metrics(self):
        raise NotImplementedError(
            "The compute_metrics() function should be implemented for your model."
            " It should return a dictionary of metrics."
        )

    def save_model(self, path, overwrite=False, **kwargs):
        self.model.save(path, overwrite, **kwargs)

    def _load_state_dict(self):
        model_state_dict_path = self.model.model.__class__.__name__+"_init_model_state_dict.pt"
        optimizer_state_dict_path = self.model.model.__class__.__name__+"_init_optimizer_state_dict.pt"
        if os.path.exists(model_state_dict_path):
            self.model.load_state_dict(torch.load(model_state_dict_path))
        if os.path.exists(optimizer_state_dict_path):
            self.optimizer.load_state_dict(torch.load(optimizer_state_dict_path))
        self.model.to(self.device)

    def _save_state_dict(self):
        model_state_dict_path = self.model.model.__class__.__name__+"_init_model_state_dict.pt"
        optimizer_state_dict_path = self.model.model.__class__.__name__+"_init_optimizer_state_dict.pt"
        if os.path.exists(model_state_dict_path):
            os.remove(model_state_dict_path)
        if os.path.exists(optimizer_state_dict_path):
            os.remove(optimizer_state_dict_path)
        self.model.to("cpu")
        torch.save(self.optimizer.state_dict(), optimizer_state_dict_path)
        torch.save(self.model.state_dict(), model_state_dict_path)
        self.model.to(self.device)
