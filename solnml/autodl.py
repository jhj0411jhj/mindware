import os
import sys
import time
import torch
import resource
import numpy as np
from ConfigSpace import ConfigurationSpace
from ConfigSpace.hyperparameters import CategoricalHyperparameter

from solnml.components.utils.constants import IMG_CLS, TEXT_CLS, OBJECT_DET
from solnml.datasets.base_dl_dataset import DLDataset
from solnml.components.ensemble.dl_ensemble.ensemble_bulider import EnsembleBuilder, ensemble_list
from solnml.components.hpo_optimizer import build_hpo_optimizer
from solnml.components.evaluators.dl_evaluator import DLEvaluator
from solnml.components.evaluators.base_dl_evaluator import get_estimator_with_parameters, TopKModelSaver, get_estimator
from solnml.components.models.img_classification.nn_utils.nn_aug.aug_hp_space import get_aug_hyperparameter_space, \
    get_test_transforms
from solnml.components.utils.config_parser import ConfigParser
from .autodl_base import AutoDLBase


rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (10240, rlimit[1]))


class AutoDL(AutoDLBase):
    def __init__(self, time_limit=300,
                 trial_num=None,
                 dataset_name='default_name',
                 task_type=IMG_CLS,
                 metric='acc',
                 include_algorithms=None,
                 ensemble_method='ensemble_selection',
                 ensemble_size=50,
                 max_epoch=150,
                 skip_profile=False,
                 config_file_path=None,
                 evaluation='holdout',
                 logging_config=None,
                 output_dir="logs/",
                 random_state=1,
                 n_jobs=1):
        super().__init__(time_limit=time_limit, trial_num=trial_num, dataset_name=dataset_name, task_type=task_type,
                         metric=metric, include_algorithms=include_algorithms, ensemble_method=ensemble_method,
                         ensemble_size=ensemble_size, max_epoch=max_epoch, config_file_path=config_file_path,
                         evaluation=evaluation, logging_config=logging_config, output_dir=output_dir,
                         random_state=random_state, n_jobs=n_jobs)
        self.skip_profile = skip_profile

    def fit(self, train_data: DLDataset, **kwargs):
        _start_time = time.time()

        if self.task_type == IMG_CLS:
            self.image_size = kwargs['image_size']

        if self.config_file_path is not None:
            config_parser = ConfigParser(logger=self.logger)
            self.update_cs = config_parser.read(self.config_file_path)

        for estimator_id in self.include_algorithms:
            cs = self.get_model_config_space(estimator_id)
            default_config = cs.get_default_configuration()
            cs.seed(self.seed)

            hpo_evaluator = DLEvaluator(default_config,
                                        self.task_type,
                                        max_epoch=self.max_epoch,
                                        scorer=self.metric,
                                        dataset=train_data,
                                        device=self.device,
                                        seed=self.seed, **kwargs)
            optimizer = build_hpo_optimizer(self.evaluation_type, hpo_evaluator, cs,
                                            output_dir=self.output_dir,
                                            per_run_time_limit=100000,
                                            trials_per_iter=1,
                                            seed=self.seed, n_jobs=self.n_jobs)
            self.solvers[estimator_id] = optimizer
            self.evaluators[estimator_id] = hpo_evaluator

        # TODO: For first-time user, download pretrained params here!
        num_train_samples = train_data.get_num_train_samples()

        # Execute profiling procedure.
        algorithm_candidates = self.include_algorithms.copy()
        if not self.skip_profile:
            algorithm_candidates = self.profile_models(num_train_samples)
            if len(algorithm_candidates) == 0:
                self.logger.error('After profiling, no arch is in the candidates!')
                sys.exit(1)
            else:
                self.logger.info('After profiling, arch candidates={%s}' % ','.join(algorithm_candidates))

        # Execute neural architecture selection.
        self.logger.info('Before NAS, arch candidates={%s}' % ','.join(algorithm_candidates))

        dl_evaluator = DLEvaluator(None,
                                   self.task_type,
                                   max_epoch=self.max_epoch,
                                   scorer=self.metric,
                                   dataset=train_data,
                                   device=self.device,
                                   seed=self.seed, **kwargs)

        algorithm_candidates = self.select_network_architectures(algorithm_candidates, dl_evaluator, num_arch=2, **kwargs)
        self.logger.info('After NAS, arch candidates={%s}' % ','.join(algorithm_candidates))

        if 'opt_method' in kwargs and kwargs['opt_method'] == 'hpo':
            self._fit_in_hpo_way(algorithm_candidates, train_data, **kwargs)
            return

        # Control flow via round robin.
        n_algorithm = len(algorithm_candidates)
        if self.trial_num is None:
            algo_id = 0
            while time.time() <= _start_time + self.time_limit:
                self.solvers[algorithm_candidates[algo_id]].iterate()
                algo_id = (algo_id + 1) % n_algorithm
        else:
            for id in self.trial_num:
                self.solvers[algorithm_candidates[id % n_algorithm]].iterate()

        # Best architecture id.
        best_scores_ = list()
        for estimator_id in algorithm_candidates:
            if estimator_id in self.solvers:
                solver_ = self.solvers[estimator_id]
                if len(solver_.perfs) > 0:
                    best_scores_.append(np.max(solver_.perfs))
                else:
                    best_scores_.append(-np.inf)
            else:
                best_scores_.append(-np.inf)

        self.best_algo_id = algorithm_candidates[np.argmax(best_scores_)]
        # Best model configuration.
        solver_ = self.solvers[self.best_algo_id]
        inc_idx = np.argmax(solver_.perfs)
        self.best_algo_config = solver_.configs[inc_idx]

        # Skip Ensemble
        if self.task_type == OBJECT_DET:
            return

        if self.ensemble_method is not None:
            stats = self.fetch_ensemble_members(algorithm_candidates)

            # Ensembling all intermediate/ultimate models found in above optimization process.
            self.es = EnsembleBuilder(stats=stats,
                                      ensemble_method=self.ensemble_method,
                                      ensemble_size=self.ensemble_size,
                                      task_type=self.task_type,
                                      max_epoch=self.max_epoch,
                                      metric=self.metric,
                                      device=self.device,
                                      output_dir=self.output_dir, **kwargs)
            self.es.fit(data=train_data)

    def fetch_ensemble_members(self, candidate_algorithms):
        stats = dict()
        # a subset of included algorithms.
        stats['include_algorithms'] = candidate_algorithms
        stats['split_seed'] = self.seed

        self.logger.info('Choose basic models for ensemble stage.')
        self.logger.info('algorithm_id, #models')
        for algo_id in stats['include_algorithms']:
            data = dict()
            leap = 2
            model_num, min_model_num = 20, 5

            hpo_eval_dict = self.solvers[algo_id].eval_dict
            topk_configs = [element[0] for element in self.evaluators[algo_id].topk_model_saver.sorted_list]

            intersection_dict = dict()
            for key in hpo_eval_dict:
                if key[1].get_dictionary() in topk_configs:
                    intersection_dict[key] = hpo_eval_dict[key]

            hpo_eval_list = filter(lambda item: item[1] != -np.inf, intersection_dict.items())
            hpo_eval_list = sorted(hpo_eval_list, key=lambda item: item[1], reverse=True)
            model_items = list()

            if len(hpo_eval_list) > 20:
                idxs = np.arange(min_model_num) * leap
                for idx in idxs:
                    model_items.append(hpo_eval_list[idx])
            else:
                model_items.extend(hpo_eval_list[:min_model_num])

            model_configs = [item[0][1] for item in model_items]
            data['model_configs'] = model_configs
            self.logger.info('%s, %d' % (algo_id, len(model_configs)))
            stats[algo_id] = data
        self.logger.info('Preparing basic models finished.')
        return stats

    def refit(self, dataset: DLDataset):
        # TODO: Bottom API changes
        if self.es is None:
            config_dict = self.best_algo_config.get_dictionary().copy()
            model_path = self.output_dir + TopKModelSaver.get_configuration_id(config_dict) + '.pt'
            # Remove the old models.
            if os.path.exists(model_path):
                os.remove(model_path)

            # Refit the models.
            _, clf = get_estimator(self.task_type, config_dict, self.max_epoch, device=self.device)
            # TODO: if train ans val are two parts, we need to merge it into one dataset.
            clf.fit(dataset.train_dataset)
            # Save to the disk.
            torch.save(clf.model.state_dict(), model_path)
        else:
            self.es.refit(dataset)

    def load_predict_data(self, test_data: DLDataset):
        if self.task_type == IMG_CLS:
            test_transforms = get_test_transforms(self.best_algo_config, image_size=self.image_size)
            test_data.load_test_data(test_transforms)
            test_data.load_data(test_transforms, test_transforms)
        else:
            test_data.load_test_data()
            test_data.load_data()

    def predict_proba(self, test_data: DLDataset, mode='test', batch_size=1, n_jobs=1):
        if self.es is None:
            self.load_predict_data(test_data)
            model_ = get_estimator_with_parameters(self.task_type, self.best_algo_config, self.max_epoch,
                                                   test_data.test_dataset, device=self.device)
            if mode == 'test':
                return model_.predict_proba(test_data.test_dataset)
            else:
                if test_data.subset_sampler_used:
                    return model_.predict_proba(test_data.train_dataset, sampler=test_data.val_sampler)
                else:
                    return model_.predict_proba(test_data.val_dataset)
        else:
            return self.es.predict(test_data, mode=mode)

    def predict(self, test_data: DLDataset, mode='test', batch_size=1, n_jobs=1):
        if self.es is None:
            self.load_predict_data(test_data)
            model_ = get_estimator_with_parameters(self.task_type, self.best_algo_config, self.max_epoch,
                                                   test_data.test_dataset, device=self.device)
            if mode == 'test':
                return model_.predict(test_data.test_dataset, batch_size=batch_size)
            else:
                if test_data.subset_sampler_used:
                    return model_.predict(test_data.train_dataset, sampler=None,
                                          batch_size=batch_size)[test_data.val_indices]
                else:
                    return model_.predict(test_data.val_dataset, batch_size=batch_size)
        else:
            return np.argmax(self.es.predict(test_data, mode=mode), axis=-1)

    def score(self, test_data: DLDataset, mode='test', metric_func=None):
        if metric_func is None:
            metric_func = self.metric
        preds = self.predict(test_data, mode=mode)
        labels = test_data.get_labels(mode=mode)
        return metric_func(labels, preds)

    def get_pipeline_config_space(self, algorithm_candidates):
        cs = ConfigurationSpace()
        estimator_choice = CategoricalHyperparameter("estimator", algorithm_candidates,
                                                     default_value=algorithm_candidates[0])
        cs.add_hyperparameter(estimator_choice)
        if self.task_type == IMG_CLS:
            aug_space = get_aug_hyperparameter_space()
            cs.add_hyperparameters(aug_space.get_hyperparameters())
            cs.add_conditions(aug_space.get_conditions())

        for estimator_id in algorithm_candidates:
            sub_cs = self.get_model_config_space(estimator_id, include_estimator=False, include_aug=False)
            parent_hyperparameter = {'parent': estimator_choice,
                                     'value': estimator_id}
            cs.add_configuration_space(estimator_id, sub_cs,
                                       parent_hyperparameter=parent_hyperparameter)
        return cs

    def _fit_in_hpo_way(self, algorithm_candidates, train_data, **kwargs):
        cs = self.get_pipeline_config_space(algorithm_candidates)
        hpo_evaluator = DLEvaluator(cs.get_default_configuration(),
                                    self.task_type,
                                    scorer=self.metric,
                                    dataset=train_data,
                                    device=self.device,
                                    image_size=self.image_size,
                                    seed=self.seed)
        optimizer = build_hpo_optimizer(self.evaluation_type, hpo_evaluator, cs,
                                        output_dir=self.output_dir,
                                        per_run_time_limit=100000,
                                        trials_per_iter=1,
                                        seed=self.seed, n_jobs=self.n_jobs)
        self.solvers['hpo_estimator'] = optimizer
        self.evaluators['hpo_estimator'] = hpo_evaluator

        # Control flow via round robin.
        _start_time = time.time()
        if self.trial_num is None:
            while time.time() <= _start_time + self.time_limit:
                self.solvers['hpo_estimator'].iterate()
        else:
            for _ in self.trial_num:
                self.solvers['hpo_estimator'].iterate()

        # Best model id.
        self.best_algo_id = 'hpo_estimator'
        # Best model configuration.
        solver_ = self.solvers[self.best_algo_id]
        inc_idx = np.argmax(solver_.perfs)
        self.best_algo_config = solver_.configs[inc_idx]

        # Skip Ensemble
        if self.task_type == OBJECT_DET:
            return

        if self.ensemble_method is not None:
            stats = self.fetch_ensemble_members(algorithm_candidates)

            # Ensembling all intermediate/ultimate models found in above optimization process.
            self.es = EnsembleBuilder(stats=stats,
                                      ensemble_method=self.ensemble_method,
                                      ensemble_size=self.ensemble_size,
                                      task_type=self.task_type,
                                      max_epoch=self.max_epoch,
                                      metric=self.metric,
                                      device=self.device,
                                      output_dir=self.output_dir, **kwargs)
            self.es.fit(data=train_data)
