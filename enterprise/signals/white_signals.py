# white_signals.py
"""Contains class factories for white noise signals. White noise signals are
defined as the class of signals that only modifies the white noise matrix `N`.
"""


import numpy as np
import scipy.sparse
import logging

from enterprise.signals import parameter, selections, signal_base, utils
from enterprise.signals.parameter import function
from enterprise.signals.selections import Selection
from enterprise.signals.utils import indices_from_slice

try:
    import fastshermanmorrison.fastshermanmorrison as fastshermanmorrison

    fsm_warning_issued = False
except ImportError:  # pragma: no cover
    fastshermanmorrison = None
    fsm_warning_issued = False

# logging.basicConfig(format="%(levelname)s: %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


def WhiteNoise(varianceFunction, selection=Selection(selections.no_selection), name=""):
    """Class factory for generic white noise signals."""

    class WhiteNoise(signal_base.Signal):
        signal_type = "white noise"
        signal_name = name
        signal_id = name

        def __init__(self, psr):
            super(WhiteNoise, self).__init__(psr)
            self.name = self.psrname + "_" + self.signal_id
            self._do_selection(psr, varianceFunction, selection)

        def _do_selection(self, psr, vfn, selection):
            sel = selection(psr)
            self._keys = sorted(sel.masks.keys())
            self._masks = [sel.masks[key] for key in self._keys]
            self._ndiag, self._params = {}, {}
            for key, mask in zip(self._keys, self._masks):
                pnames = [psr.name, name, key]
                pname = "_".join([n for n in pnames if n])
                self._ndiag[key] = vfn(pname, psr=psr)
                for param in self._ndiag[key]._params.values():
                    self._params[param.name] = param

        @property
        def ndiag_params(self):
            """Get any varying ndiag parameters."""
            return [pp.name for pp in self.params]

        @signal_base.cache_call("ndiag_params")
        def get_ndiag(self, params):
            ret = 0
            for key, mask in zip(self._keys, self._masks):
                ret += self._ndiag[key](params=params) * mask
            return signal_base.ndarray_alt(ret)

    return WhiteNoise


@function
def efac_ndiag(toaerrs, efac=1.0):
    return efac**2 * toaerrs**2


@function
def combined_ndiag(toaerrs, efac=1.0, log10_t2equad=-8):
    return efac**2 * (toaerrs**2 + 10 ** (2 * log10_t2equad))


def MeasurementNoise(
    efac=parameter.Uniform(0.5, 1.5),
    log10_t2equad=None,
    selection=Selection(selections.no_selection),
    name="",
):
    """Class factory for EFAC+EQUAD measurement noise
    (with tempo/tempo2/pint parameter convention, variance = efac^2 (toaerr^2 + t2equad^2)).
    Leave out log10_t2equad to use EFAC noise only."""

    varianceFunction = (
        efac_ndiag(efac=efac) if log10_t2equad is None else combined_ndiag(efac=efac, log10_t2equad=log10_t2equad)
    )
    BaseClass = WhiteNoise(varianceFunction, selection=selection, name=name)

    class MeasurementNoise(BaseClass):
        signal_name = "measurement_noise"
        signal_id = "measurement_noise_" + name if name else "measurement_noise"

    return MeasurementNoise


@function
def tnequad_ndiag(toas, log10_tnequad=-8):
    return np.ones_like(toas) * 10 ** (2 * log10_tnequad)


def TNEquadNoise(log10_tnequad=parameter.Uniform(-10, -5), selection=Selection(selections.no_selection), name=""):
    """Class factory for TNEQUAD type measurement noise (legacy, not multiplied by EFAC)."""

    varianceFunction = tnequad_ndiag(log10_tnequad=log10_tnequad)
    BaseClass = WhiteNoise(varianceFunction, selection=selection, name=name)

    class TNEquadNoise(BaseClass):
        signal_name = "tnequad"
        signal_id = "tnequad_" + name if name else "tnequad"

    return TNEquadNoise


def EquadNoise(*args, **kwargs):
    raise NotImplementedError(
        "EquadNoise was removed in enterprise v3.3."
        " Use MeasurementNoise to implement tempo/tempo2/pint definition [efac^2 (toaerr^2 + t2equad^2)]"
        " or TNEquadNoise to obtain legacy enterprise definition for EQUAD only [tnequad^2]."
    )


def EcorrKernelNoise(
    log10_ecorr=parameter.Uniform(-10, -5),
    selection=Selection(selections.no_selection),
    method="fast-sherman-morrison",
    name="",
):
    r"""Class factory for ECORR type noise.

    :param log10_ecorr: ``Parameter`` type for log10 or ecorr parameter.
    :param selection:
        ``Selection`` object specifying masks for backends, time segments, etc.
    :param method: Method for computing noise covariance matrix.
        Options include `fast-sherman-morrison`, `sherman-morrison`, `sparse`,
        and `block`

    :return: ``EcorrKernelNoise`` class.

    ECORR is a noise signal that is used for data with multi-channel TOAs
    that are nearly simultaneous in time. It is a white noise signal that
    is uncorrelated epoch to epoch but completely correlated for TOAs in a
    given observing epoch.

    For this implementation we use this covariance matrix as part of the
    white noise covariance matrix :math:`N`. It can be seen from above that
    this covariance is block diagonal, thus allowing us to exploit special
    methods to make matrix manipulations easier.

    In this signal implementation we offer three methods of performing these
    matrix operations:

    fast-sherman-morrison
        Uses the `Sherman-Morrison`_ forumla to compute the matrix
        inverse and other matrix operations. **Note:** This method can only
        be used for covariances that make up ECorrKernelNoise, :math:`uv^T`.
        This version is Cython optimized.

    sherman-morrison
        Uses the `Sherman-Morrison`_ forumla to compute the matrix
        inverse and other matrix operations. **Note:** This method can only
        be used for covariances that can be constructed by the outer product
        of two vectors, :math:`uv^T`.

    sparse
        Uses `Scipy Sparse`_ matrices to construct the block diagonal
        covariance matrix and perform matrix operations.

    block
        Uses a custom scheme that uses the individual blocks from the block
        diagonal matrix to perform fast matrix inverse and other solve
        operations.

    .. note:: The sherman-morrison method is the fastest, followed by the block
        and then sparse methods, however; the block and sparse methods are more
        general and should be used if sub-classing this signal for more
        complicated blocks.

    .. _Sherman-Morrison: https://en.wikipedia.org/wiki/Sherman-Morrison_formula
    .. _Scipy Sparse: https://docs.scipy.org/doc/scipy-0.18.1/reference/sparse.html
    .. # noqa E501

    """

    global fsm_warning_issued

    if method not in ["fast-sherman-morrison", "sherman-morrison", "block", "sparse"]:
        msg = "EcorrKernelNoise does not support method: {}".format(method)
        raise TypeError(msg)

    if method == "fast-sherman-morrison" and fastshermanmorrison is None and not fsm_warning_issued:  # pragma: no cover
        msg = "Package `fastshermanmorrison` not installed. Fallback to sherman-morrison"
        logger.warning(msg)
        fsm_warning_issued = True

    class EcorrKernelNoise(signal_base.Signal):
        signal_type = "white noise"
        signal_name = "ecorr_" + method
        signal_id = "_".join(["ecorr", name, method]) if name else "_".join(["ecorr", method])

        def __init__(self, psr):
            super(EcorrKernelNoise, self).__init__(psr)
            self.name = self.psrname + "_" + self.signal_id

            sel = selection(psr)
            self._params, self._masks = sel("log10_ecorr", log10_ecorr)
            keys = sorted(self._masks.keys())
            masks = [self._masks[key] for key in keys]

            Umats = []
            for key, mask in zip(keys, masks):
                Umats.append(utils.create_quantization_matrix(psr.toas[mask], nmin=2)[0])

            nepoch = sum(U.shape[1] for U in Umats)
            U = np.zeros((len(psr.toas), nepoch))
            self._slices = {}
            self._idxs = {}
            netot = 0
            for ct, (key, mask) in enumerate(zip(keys, masks)):
                nn = Umats[ct].shape[1]
                U[mask, netot : nn + netot] = Umats[ct]
                self._slices.update({key: utils.quant2ind(U[:, netot : nn + netot])})
                netot += nn

            self._idxs.update(
                {key: [indices_from_slice(slc) for slc in slices] for (key, slices) in self._slices.items()}
            )

            # initialize sparse matrix
            self._setup(psr)

        @property
        def ndiag_params(self):
            """Get any varying ndiag parameters."""
            return [pp.name for pp in self.params]

        @signal_base.cache_call("ndiag_params")
        def get_ndiag(self, params):
            if method == "sherman-morrison":
                return self._get_ndiag_sherman_morrison(params)
            elif method == "fast-sherman-morrison":
                if fastshermanmorrison:
                    return self._get_ndiag_fast_sherman_morrison(params)
                else:  # pragma: no cover
                    return self._get_ndiag_sherman_morrison(params)
            elif method == "sparse":
                return self._get_ndiag_sparse(params)
            elif method == "block":
                return self._get_ndiag_block(params)

        def _setup(self, psr):
            if method == "sparse":
                self._setup_sparse(psr)

        def _setup_sparse(self, psr):
            Ns = scipy.sparse.csc_matrix((len(psr.toas), len(psr.toas)))
            for key, idxs in self._idxs.items():
                for idx in idxs:
                    if len(idx) > 1:
                        Ns[np.ix_(idx, idx)] = 1.0
            self._Ns = signal_base.csc_matrix_alt(Ns)

        def _get_ndiag_sparse(self, params):
            for p in self._params:
                for idx in self._idxs[p]:
                    if len(idx) > 1:
                        self._Ns[np.ix_(idx, idx)] = 10 ** (2 * self.get(p, params))
            return self._Ns

        def _get_ndiag_sherman_morrison(self, params):
            slices, jvec = self._get_jvecs(params)
            return signal_base.ShermanMorrison(jvec, slices)

        def _get_ndiag_fast_sherman_morrison(self, params):
            slices, jvec = self._get_jvecs(params)
            return fastshermanmorrison.FastShermanMorrison(jvec, slices)

        def _get_ndiag_block(self, params):
            idxs, jvec = self._get_jvecs(params)
            blocks = []
            for jv, idx in zip(jvec, idxs):
                nb = len(idx)
                blocks.append(np.ones((nb, nb)) * jv)
            return signal_base.BlockMatrix(blocks, idxs)

        def _get_jvecs(self, params):
            idxs = sum([self._idxs[key] for key in sorted(self._idxs.keys())], [])
            jvec = np.concatenate(
                [np.ones(len(self._idxs[key])) * 10 ** (2 * self.get(key, params)) for key in sorted(self._idxs.keys())]
            )
            return (idxs, jvec)

    return EcorrKernelNoise
