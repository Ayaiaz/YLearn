import inspect
from copy import deepcopy

import numpy as np
import pandas as pd
from sklearn.preprocessing import LabelEncoder

from ylearn import sklearn_ex as skex
from ylearn.causal_discovery import BaseDiscovery
from ylearn.causal_model import CausalModel, CausalGraph
from ylearn.utils import logging
from .utils import _align_task_to_first, _select_by_task, _empty, _not_empty, _is_number

logger = logging.get_logger(__name__)


class Identifier:
    def identify_treatment(self, data, outcome, discrete_treatment, count_limit, excludes=None):
        raise NotImplemented()

    def identify_aci(self, data, outcome, treatment):
        raise NotImplemented()


class DefaultIdentifier(Identifier):
    def identify_treatment(self, data, outcome, discrete_treatment, count_limit, excludes=None):
        X = data.copy()
        y = X.pop(outcome)

        if excludes is not None and len(excludes) > 0:
            X = X[[c for c in X.columns.tolist() if c not in excludes]]

        tf = skex.FeatureImportancesSelectionTransformer(
            strategy='number', number=X.shape[1], data_clean=False)
        tf.fit(X, y)
        selected = tf.selected_features_

        if discrete_treatment is None:
            treatment = _align_task_to_first(data, selected, count_limit)
        else:
            treatment = _select_by_task(data, selected, count_limit, discrete_treatment)

        return treatment

    def identify_aci(self, data, outcome, treatment):
        adjustment, instrument = None, None

        covariate = [c for c in data.columns.tolist() if c != outcome and c not in treatment]

        return adjustment, covariate, instrument


class IdentifierWithDiscovery(DefaultIdentifier):
    def __init__(self, method=None, random_state=None, **kwargs):
        self.method = method
        self.random_state = random_state
        self.discovery_options = kwargs.copy()
        self.causation_matrix_ = None

    def _discovery(self, data, outcome):
        logger.info('discovery causation')

        X = data.copy()
        y = X.pop(outcome)

        if not _is_number(y.dtype):
            y = LabelEncoder().fit_transform(y)

        # preprocessor = skex.general_preprocessor(number_scaler=True)
        preprocessor = skex.general_preprocessor()
        X = preprocessor.fit_transform(X, y)
        X[outcome] = y
        return self._discovery_causation(X)

    def _discovery_causation(self, X):
        """
        learn X and return casual matrix
        """
        raise NotImplementedError()

    def identify_treatment(self, data, outcome, discrete_treatment, count_limit, excludes=None):
        causation = self._discovery(data, outcome)
        assert isinstance(causation, pd.DataFrame) and outcome in causation.columns.tolist()

        treatment = causation[outcome].abs().sort_values(ascending=False)
        treatment = [i for i in treatment.index if treatment[i] > 0 and i != outcome]
        if excludes is not None:
            treatment = [t for t in treatment if t not in excludes]

        if len(treatment) > 0:
            if discrete_treatment is None:
                treatment = _align_task_to_first(data, treatment, count_limit)
            else:
                treatment = _select_by_task(data, treatment, count_limit, discrete_treatment)
        else:
            logger.info(f'Not found treatment with causal discovery, so identify treatment by default')
            treatment = super().identify_treatment(data, outcome, discrete_treatment, count_limit, excludes=excludes)

        self.causation_matrix_ = causation

        return treatment

    def identify_aci(self, data, outcome, treatment):
        if self.causation_matrix_ is None:
            self.causation_matrix_ = self._discovery(data, outcome)
        causation = self.causation_matrix_
        # threshold = causation.values.diagonal().max()
        threshold = min(np.quantile(causation.values.diagonal(), 0.8),
                        np.mean(causation.values))

        if np.isnan(threshold):
            return super().identify_aci(data, outcome, treatment)

        m = BaseDiscovery.matrix2dict(causation, threshold=threshold)

        if self.method == 'straight':
            covariate, instrument = self._identify_ci_straight_forward(
                m, data, outcome, treatment)
        else:
            covariate, instrument = self._identify_ci_with_causal_model(
                m, data, outcome, treatment, method=self.method)

        if _empty(covariate):
            logger.info('Not found covariate by discovery, so setup it as default')
            covariate = [c for c in data.columns.tolist()
                         if c != outcome and c not in treatment and (_empty(instrument) or c not in instrument)]

        if logger.is_info_enabled():
            if _not_empty(instrument):
                logger.info(f'found instrument: {instrument}')
            logger.info(f'found covariate: {covariate}')

        if _empty(instrument):
            instrument = None

        return None, covariate, instrument

    @staticmethod
    def _identify_ci_with_causal_model(causal_dict, data, outcome, treatment, method=None):
        if method is None:
            method = ('backdoor', 'simple')

        cg = CausalGraph(causal_dict)
        cm = CausalModel(cg)
        try:
            instrument = cm.get_iv(treatment[0], outcome)
            if _not_empty(instrument):
                instrument = [c for c in instrument if c != outcome and c not in treatment]
            for x in treatment[1:]:
                if _empty(instrument):
                    break
                iv = cm.get_iv(x, outcome)
                if _empty(iv):
                    instrument = None
                    break
                else:
                    iv = [c for c in iv if c != outcome and c not in treatment]
                    instrument = list(set(instrument).intersection(set(iv)))
        except Exception as e:
            logger.warn(e)
            instrument = []

        ids = cm.identify(treatment, outcome, identify_method=method)
        covariate = list(set(ids['backdoor'][0]))
        covariate = [c for c in covariate if c != outcome and c not in treatment]
        if not _empty(instrument):
            covariate = [c for c in covariate if c not in instrument]

        return covariate, instrument

    @staticmethod
    def _identify_ci_straight_forward(causal_dict, data, outcome, treatment):
        # expand causal dict
        m0 = deepcopy(causal_dict)
        for _ in range(data.shape[1] + 1):
            flag = 0
            for k, v in causal_dict.items():
                for ki in v:
                    diff = set(m0[ki]).difference(set(m0[k]))
                    if diff:
                        m0[k] += list(diff)
                        flag += 1
            if flag == 0:
                break

        if isinstance(treatment, str):
            treatment = [treatment, ]
        xy = treatment + [outcome]
        var_x = [k for k, v in m0.items() if
                 any(map(lambda t: t in v and k not in xy, treatment))]
        var_y = [k for k, v in m0.items() if outcome in v and k not in xy]

        instrument = [c for c in var_x if c in set(var_x).difference(set(var_y)) and c not in xy]
        covariate = [c for c in var_y if c not in instrument and c not in xy]

        return covariate, instrument


class IdentifierWithNotears(IdentifierWithDiscovery):
    def _discovery_causation(self, X):
        from ylearn.causal_discovery import CausalDiscovery

        options = dict(random_state=self.random_state)
        if self.discovery_options is not None:
            options.update(self.discovery_options)

        cd = CausalDiscovery(**options)
        return cd(X)


class IdentifierWithLearner(IdentifierWithDiscovery):
    def __init__(self, learner, **kwargs):
        assert callable(learner)

        params = inspect.signature(learner).parameters
        assert len(params.keys()) > 0, 'learner should be able to accept one DataFrame as parameter. '

        self.learner = learner

        super().__init__(  **kwargs)

    def _discovery_causation(self, X):
        assert isinstance(X, pd.DataFrame)
        x_shape = X.shape
        columns = X.columns.tolist()
        matrix = self.learner(X)

        assert isinstance(matrix, (pd.DataFrame, np.ndarray)), \
            'causal matrix should be numpy.ndarray or pandas.DataFrame'
        assert matrix.shape == (x_shape[1], x_shape[1])

        if isinstance(matrix, np.ndarray):
            matrix = pd.DataFrame(matrix, columns=columns, index=columns)
        return matrix
