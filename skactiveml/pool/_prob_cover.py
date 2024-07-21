"""
Module implementing `ProbCover`, which is a deep active learning strategy
suited for low budgets.
"""

import numpy as np

from sklearn.metrics import pairwise_distances

from ..base import SingleAnnotatorPoolQueryStrategy
from ..utils import MISSING_LABEL, is_unlabeled, rand_argmax, check_scalar
from sklearn.cluster import KMeans
from sklearn.utils.validation import column_or_1d


class ProbCover(SingleAnnotatorPoolQueryStrategy):
    """Probability Coverage

    This class implements the Probability Coverage (ProbCover) query strategy
    [1], which aims at maximizing the probability coverage in a meaninfulg
    sample embedding space.

    Parameters
    ----------
    cluster_algo : ClusterMixin.__class__ (default=KMeans)
        The cluster algorithm to be used for determining the best delta value.
    cluster_algo_dict : dict, optional (default=None)
        The parameters passed to the clustering algorithm `cluster_algo`,
        excluding the parameter for the number of clusters.
    n_cluster_param_name : string (default="n_clusters")
        The name of the parameter for the number of clusters.
    missing_label : scalar or string or np.nan or None, default=np.nan
        Value to represent a missing label.
    random_state : None or int or np.random.RandomState
        The random state to use.

    References
    ----------
    [1] Yehuda, Ofer, Avihu Dekel, Guy Hacohen, and Daphna Weinshall. "Active
        Learning Through a Covering Lens." NeurIPS, 2022.
    """

    def __init__(
        self,
        n_classes=None,
        deltas=None,
        alpha=0.95,
        cluster_algo=KMeans,
        cluster_algo_dict=None,
        n_cluster_param_name="n_clusters",
        missing_label=MISSING_LABEL,
        random_state=None,
    ):
        super().__init__(
            missing_label=missing_label, random_state=random_state
        )
        self.deltas = deltas
        self.alpha = alpha
        self.n_classes = n_classes
        self.cluster_algo = cluster_algo
        self.cluster_algo_dict = cluster_algo_dict
        self.n_cluster_param_name = n_cluster_param_name

    def query(
        self,
        X,
        y,
        candidates=None,
        batch_size=1,
        return_utilities=False,
    ):
        """Query the next samples to be labeled

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
            Training data set, usually complete, i.e. including the labeled and
            unlabeled samples
        y : array-like of shape (n_samples, )
            Labels of the training data set (possibly including unlabeled ones
            indicated by self.missing_label)
        candidates : None or array-like of shape (n_candidates), dtype = int or
        array-like of shape (n_candidates, n_features), optional (default=None)
            If candidates is None, the unlabeled samples from (X, y)
            are considered as candidates.
            If candidates is of shape (n_candidates) and of type int,
            candidates is considered as a list of the indices of the samples in
            (X, y).
            If candidates is of shape (n_candidates, n_features), the
            candidates are directly given in the input candidates (not
            necessarily contained in X).
        batch_size : int, optional(default=1)
            The number of samples to be selects in one AL cycle.
        return_utilities : bool, optional(default=False)
            If True, also return the utilities based on the query strategy

        Returns
        ----------
        query_indices : numpy.ndarry of shape (batch_size)
            The query_indices indicate for which candidate sample a label is
            to queried, e.g., `query_indices[0]` indicates the first selected
            sample.
            If candidates in None or of shape (n_candidates), the indexing
            refers to samples in X.
            If candidates is of shape (n_candidates, n_features), the indexing
            refers to samples in candidates.
        utilities : numpy.ndarray of shape (batch_size, n_samples) or
            numpy.ndarray of shape (batch_size, n_candidates)
            The utilities of samples for selecting each sample of the batch.
            Here, utilities mean the typicality in the considered cluster.
            If candidates is None or of shape (n_candidates), the indexing
            refers to samples in X.
            If candidates is of shape (n_candidates, n_features), the indexing
            refers to samples in candidates.
        """
        # Check parameters.
        X, y, candidates, batch_size, return_utilities = self._validate_data(
            X, y, candidates, batch_size, return_utilities, reset=True
        )
        _, _ = self._transform_candidates(
            candidates, X, y, enforce_mapping=True
        )
        is_candidate = is_unlabeled(y, missing_label=self.missing_label)
        check_scalar(
            self.alpha,
            "alpha",
            min_val=0,
            max_val=1,
            min_inclusive=False,
            max_inclusive=False,
            target_type=float,
        )
        if self.deltas is None:
            deltas = np.arange(0.2, 2.2, 0.2)
        else:
            deltas = column_or_1d(self.deltas, dtype=float)
            if (deltas <= 0).any():
                raise ValueError(
                    "`deltas` must contain strictly positive floats."
                )

        if not (
            isinstance(self.cluster_algo_dict, dict)
            or self.cluster_algo_dict is None
        ):
            raise TypeError(
                "Pass a dictionary with corresponding parameter names and "
                "values according to the `init` function of `cluster_algo`."
            )
        cluster_algo_dict = (
            {}
            if self.cluster_algo_dict is None
            else self.cluster_algo_dict.copy()
        )

        if not isinstance(self.n_cluster_param_name, str):
            raise TypeError("`n_cluster_param_name` supports only string.")

        # Compute distances between each pair of observed samples.
        distances = pairwise_distances(X)

        # Compute the maximum `delta` value satisfying a purity >= `alpha`.
        if len(deltas) == 1:
            self.delta_max_ = deltas[0]
        elif not hasattr(self, "delta_max_"):
            cluster_algo_dict[self.n_cluster_param_name] = self.n_classes
            cluster_obj = self.cluster_algo(**cluster_algo_dict)
            y_cluster = cluster_obj.fit_predict(X)
            is_impure = y_cluster[:, None] != y_cluster
            for delta in deltas:
                edges = distances < delta
                purity = 1 - (edges * is_impure).any(axis=1).mean()
                if purity < self.alpha:
                    break
                self.delta_max_ = delta
        edges = distances <= self.delta_max_

        query_indices = np.full(batch_size, fill_value=-1, dtype=int)
        utilities = np.full((batch_size, len(X)), fill_value=np.nan)
        for b in range(batch_size):
            # Step (ii) in [1]: Remove incoming edges for covered samples.
            is_covered = edges[~is_candidate].any(axis=0)
            edges[:, is_covered] = False
            # Step (i) in [1]: Query the sample with the highest out-degree.
            utilities[b][is_candidate] = edges[is_candidate].sum(axis=1)
            idx = rand_argmax(utilities[b], random_state=self.random_state_)[0]
            is_candidate[idx] = False
            query_indices[b] = idx

        if return_utilities:
            return query_indices, utilities
        else:
            return query_indices


def _construct_graph(X, is_candidate, delta):
    """
    Calculation the typicality of uncovers samples in X.

    Parameters
    ----------
    X : array-like of shape (n_samples, n_features)
        Training data set including the labeled and unlabeled samples.
    is_candidate : np.ndarray of shape (n_candidates,)
       Index array that maps `candidates` to `X_for_cluster`.
    delta : float > 0
        Ball radius centred at a sample.

    Returns
    -------
    vertices : numpy.ndarray of shape (n_X)
        The typicality of all uncovered samples in X
    """
    pass
