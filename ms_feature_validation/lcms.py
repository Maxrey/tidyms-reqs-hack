"""
Functions and objects for working with LC-MS data

Objects
-------
Chromatogram
MSSpectrum
Roi

"""

import numpy as np
import pandas as pd
import pyopenms
from scipy.interpolate import interp1d
from typing import Optional, Iterable, Tuple, Union, List, Callable
from . import peaks
import bokeh.plotting
from bokeh.palettes import Set3
from bokeh.models import ColumnDataSource
from bokeh.models import HoverTool
from collections import namedtuple

from .utils import find_closest

msexperiment = Union[pyopenms.MSExperiment, pyopenms.OnDiscMSExperiment]


def reader(path: str, on_disc: bool = True):
    """
    Load `path` file into an OnDiskExperiment. If the file is not indexed, load
    the file.

    Parameters
    ----------
    path : str
        path to read mzML file from.
    on_disc : bool
        if True doesn't load the whole file on memory.

    Returns
    -------
    pyopenms.OnDiskMSExperiment or pyopenms.MSExperiment
    """
    if on_disc:
        try:
            exp_reader = pyopenms.OnDiscMSExperiment()
            exp_reader.openFile(path)
        except RuntimeError:
            msg = "{} is not an indexed mzML file, switching to MSExperiment"
            print(msg.format(path))
            exp_reader = pyopenms.MSExperiment()
            pyopenms.MzMLFile().load(path, exp_reader)
    else:
        exp_reader = pyopenms.MSExperiment()
        pyopenms.MzMLFile().load(path, exp_reader)
    return exp_reader


def chromatogram(msexp: msexperiment, mz: Iterable[float],
                 window: float = 0.005, start: Optional[int] = None,
                 end: Optional[int] = None,
                 accumulator: str = "sum") -> Tuple[np.ndarray, np.ndarray]:
    """
    Computes extracted ion chromatograms for a list of m/z values from raw
    data.

    Parameters
    ----------
    msexp : MSExp or OnDiskMSExp.
    mz : iterable[float]
        mz values used to build the EICs.
    start : int, optional
        first scan to build the chromatogram
    end : int, optional
        last scan to build the chromatogram.
    window : float.
               Tolerance to build the EICs.
    accumulator : {"sum", "mean"}
        "mean" divides the intensity in the EIC using the number of points in
        the window.
    Returns
    -------
    rt : array of retention times
    eic : array with rows of EICs.
    """
    if not isinstance(mz, np.ndarray):
        mz = np.array(mz)
    mz_intervals = (np.vstack((mz - window, mz + window))
                    .T.reshape(mz.size * 2))
    nsp = msexp.getNrSpectra()

    if start is None:
        start = 0

    if end is None:
        end = nsp

    eic = np.zeros((mz.size, end - start))
    rt = np.zeros(end - start)
    for ksp in range(start, end):
        sp = msexp.getSpectrum(ksp)
        rt[ksp] = sp.getRT()
        mz_sp, int_sp = sp.get_peaks()
        ind_sp = np.searchsorted(mz_sp, mz_intervals)
        # check if the slices aren't empty
        has_mz = (ind_sp[1::2] - ind_sp[::2]) > 0
        # elements added at the end of mz_sp raise IndexError
        ind_sp[ind_sp >= int_sp.size] = int_sp.size - 1
        eic[:, ksp] = np.where(has_mz,
                                         np.add.reduceat(int_sp, ind_sp)[::2],
                                         0)
        if accumulator == "mean":
            norm = ind_sp[1::2] - ind_sp[::2]
            norm[norm == 0] = 1
            eic[:, ksp] = eic[:, ksp] / norm
        elif accumulator == "sum":
            pass
        else:
            msg = "accumulator possible values are `mean` and `sum`."
            raise ValueError(msg)
    return rt, eic


def accumulate_spectra(msexp: msexperiment, start: int,
                       end: int, subtract: Optional[Tuple[int, int]] = None,
                       kind: str = "linear",
                       accumulator: str = "sum"
                       ) -> Tuple[np.ndarray, np.ndarray]:
    """
    accumulates a spectra into a single spectrum.

    Parameters
    ----------
    msexp : pyopenms.MSExperiment, pyopenms.OnDiskMSExperiment
    start : int
        start slice for scan accumulation
    end : int
        end slice for scan accumulation.
    kind : str
        kind of interpolator to use with scipy interp1d.
    subtract : None or Tuple[int], left, right
        Scans regions to substract. `left` must be smaller than `start` and
        `right` greater than `end`.
    accumulator : {"sum", "mean"}

    Returns
    -------
    accum_mz : array of m/z values
    accum_int : array of intensities.
    """
    accumulator_functions = {"sum": np.sum, "mean": np.mean}
    accumulator = accumulator_functions[accumulator]

    if subtract is not None:
        if (subtract[0] > start) or (subtract[-1] < end):
            raise ValueError("subtract region outside scan region.")
    else:
        subtract = (start, end)

    # interpolate accumulate and substract regions
    rows = subtract[1] - subtract[0]
    mz_ref = _get_mz_roi(msexp, subtract)
    interp_int = np.zeros((rows, mz_ref.size))
    for krow, scan in zip(range(rows), range(*subtract)):
        mz_scan, int_scan = msexp.getSpectrum(scan).get_peaks()
        interpolator = interp1d(mz_scan, int_scan, kind=kind)
        interp_int[krow, :] = interpolator(mz_ref)

    # subtract indices to match interp_int rows
    start = start - subtract[0]
    end = end - subtract[0]
    subtract = 0, subtract[1] - subtract[0]

    accum_int = (accumulator(interp_int[start:end], axis=0)
                 - accumulator(interp_int[subtract[0]:start], axis=0)
                 - accumulator(interp_int[end:subtract[1]], axis=0))
    accum_mz = mz_ref

    return accum_mz, accum_int


def _get_mz_roi(ms_experiment, scans):
    """
    make an mz array with regions of interest in the selected scans.

    Parameters
    ----------
    ms_experiment : pyopenms.MSEXperiment, pyopenms.OnDiskMSExperiment
    scans : tuple[int] : start, end

    Returns
    -------
    mz_ref : array
    """
    mz_0, _ = ms_experiment.getSpectrum(scans[0]).get_peaks()
    mz_min = mz_0.min()
    mz_max = mz_0.max()
    mz_res = np.diff(mz_0).min()
    mz_ref = np.arange(mz_min, mz_max, mz_res)
    roi = np.zeros(mz_ref.size + 1)
    # +1 used to prevent error due to mz values bigger than mz_max
    for k in range(*scans):
        curr_mz, _ = ms_experiment.getSpectrum(k).get_peaks()
        roi_index = np.searchsorted(mz_ref, curr_mz)
        roi[roi_index] += 1
    roi = roi.astype(bool)
    return mz_ref[roi[:-1]]


def make_widths_lc(mode: str) -> np.ndarray:
    """
    Create an array of widths to use in CWT peak picking of LC data.

    Parameters
    ----------
    mode: {"hplc", "uplc"}

    Returns
    -------
    widths: array
    """
    if mode == "uplc":
        min_width = 1
        middle = 15
        max_width = 60
    elif mode == "hplc":
        min_width = 1
        middle = 30
        max_width = 90
    else:
        msg = "Valid modes are `hplc` or `uplc`."
        raise ValueError(msg)

    # [:-1] prevents repeated value
    widths = np.hstack((np.linspace(min_width, middle, 20)[:-1],
                        np.linspace(middle, max_width, 20)))
    # min_x_distance = np.diff(x).min()
    # n = int((max_width - min_x_distance) / min_x_distance)
    # first_half = np.linspace(min_x_distance, 10 * min_x_distance, 40)
    # second_half = np.linspace(11 * min_x_distance, max_width, n - 10)
    # widths = np.hstack((first_half, second_half))

    return widths


def make_widths_ms(mode: str) -> np.ndarray:
    """
    Create an array of widths to use in CWT peak picking of MS data.

    Parameters
    ----------
    mode : {"qtof", "orbitrap"}

    Returns
    -------
    widths : array
    """
    if mode == "qtof":
        min_width = 0.005
        middle = 0.1
        max_width = 0.2
    elif mode == "qtof":
        min_width = 0.0005
        middle = 0.001
        max_width = 0.005
    else:
        msg = "mode must be `orbitrap` or `qtof`"
        raise ValueError(msg)
    # [:-1] prevents repeated value
    widths = np.hstack((np.linspace(min_width, middle, 20)[:-1],
                        np.linspace(middle, max_width, 10)))
    return widths


def get_lc_cwt_params(mode: str) -> dict:
    """
    Return sane default values for performing CWT based peak picking on LC data.

    Parameters
    ----------
    mode : {"hplc", "uplc"}
        HPLC assumes typical experimental conditions for HPLC experiments:
        longer columns with particle size greater than 3 micron. UPLC is for
        data acquired with short columns with particle size lower than 3 micron.

    Returns
    -------
    cwt_params : dict
        parameters to pass to .peak.pick_cwt function.
    """
    cwt_params = {"snr": 10, "bl_ratio": 2, "min_length": None,
                  "max_distance": None, "gap_thresh": 1}

    if mode == "hplc":
        cwt_params["min_width"] = 10
        cwt_params["max_width"] = 90
    elif mode == "uplc":
        cwt_params["min_width"] = 5
        cwt_params["max_width"] = 60
    else:
        msg = "`mode` must be `hplc` or `uplc`"
        raise ValueError(msg)
    return cwt_params


def get_ms_cwt_params(mode: str) -> dict:
    """
    Return sane default values for performing CWT based peak picking on MS data.

    Parameters
    ----------
    mode : {"qtof", "orbitrap"}
        qtof assumes a peak width in the range of 0.01-0.05 Da. `orbitrap`
        assumes a peak width in the range of 0.001-0.005 Da.
        TODO: add ppm scale

    Returns
    -------
    cwt_params : dict
        parameters to pass to .peak.pick_cwt function.
    """
    cwt_params = {"snr": 10, "bl_ratio": 2, "min_length": None,
                  "max_distance": None, "gap_thresh": 1}

    if mode == "qtof":
        cwt_params["min_width"] = 0.01
        cwt_params["max_width"] = 0.2
    elif mode == "orbitrap":
        cwt_params["min_width"] = 0.0005
        cwt_params["max_width"] = 0.005
    else:
        msg = "`mode` must be `qtof` or `orbitrap`"
        raise ValueError(msg)
    return cwt_params


def get_roi_params(separation: str = "uplc", instrument: str = "qtof"):
    """
    Creates a dictionary with recommended parameters for the make_roi function
    in different use cases.

    Parameters
    ----------
    separation : {"uplc", "hplc"}
        Mode in which the data was acquired. Used to set minimum length of the
        roi and number of missing values.
    instrument : {"qtof", "orbitrap"}
        Type of MS instrument. Used to set the tolerance.

    Returns
    -------
    roi_parameters : dict
    """
    roi_params = {"min_intensity": 500, "multiple_match": "reduce"}

    if separation == "uplc":
        roi_params.update({"max_missing": 1, "min_length": 10})
    elif separation == "hplc":
        roi_params.update({"max_missing": 1, "min_length": 20})
    else:
        msg = "valid `mode` are uplc and hplc"
        raise ValueError(msg)

    if instrument == "qtof":
        roi_params.update({"tolerance": 0.01})
    elif instrument == "orbitrap":
        roi_params.update({"tolerance": 0.005})
    else:
        msg = "valid `ms_mode` are qtof and orbitrap"
        raise ValueError(msg)

    roi_params["mode"] = separation

    return roi_params


def find_isotopic_distribution_aux(mz: np.ndarray, mz_ft: float,
                                   q: int, n_isotopes: int,
                                   tol: float):
    """
    Finds the isotopic distribution for a given charge state. Auxiliary function
    to find_isotopic_distribution.
    Isotopes are searched based on the assumption that the mass difference
    is due to the presence of a 13C atom.

    Parameters
    ----------
    mz : numpy.ndarray
        List of peaks
    mz_ft : float
        Monoisotopic mass
    q : charge state of the ion
    n_isotopes : int
        Number of isotopes to search in the distribution
    tol: float
        Mass tolerance, in absolute units

    Returns
    -------
    match_ind : np.ndarray
        array of indices for the isotopic distribution.
    """
    mono_index = find_closest(mz, mz_ft)
    mz_mono = mz[mono_index]
    if abs(mz_mono - mz_ft) > tol:
        match_ind = np.array([])
    else:
        dm = 1.003355
        mz_theoretic = mz_mono + np.arange(n_isotopes) * dm / q
        closest_ind = find_closest(mz, mz_theoretic)
        match_ind = np.where(np.abs(mz[closest_ind] - mz_theoretic) <= tol)[0]
        match_ind = closest_ind[match_ind]
    return match_ind


def find_isotopic_distribution(mz: np.ndarray, mz_mono: float,
                               q_max: int, n_isotopes: int,
                               tol: float):
    """
    Finds the isotopic distribution within charge lower than q_max.
    Isotopes are searched based on the assumption that the mass difference
    is due to the presence of a 13C atom. If multiple charge states are
    compatible with an isotopic distribution, the charge state with the largest
    number of isotopes detected is kept.

    Parameters
    ----------
    mz : numpy.ndarray
        List of peaks
    mz_mono : float
        Monoisotopic mass
    q_max : int
        max charge to analyze
    n_isotopes : int
        Number of isotopes to search in the distribution
    tol : float
        Mass tolerance, in absolute units

    Returns
    -------
    best_peaks: numpy.ndarray

    """
    best_peaks = np.array([], dtype=int)
    n_peaks = 0
    for q in range(1, q_max + 1):
        tmp = find_isotopic_distribution_aux(mz, mz_mono, q,
                                             n_isotopes, tol)
        if tmp.size > n_peaks:
            best_peaks = tmp
    return best_peaks


class Chromatogram:
    """
    Representation of a chromatogram. Manages plotting and peak picking.

    Attributes
    ----------
    spint : array
        intensity in each scan
    mz : float
        mz value used to build the chromatogram.
    start : int, optional
        scan number where chromatogram starts
    end : int, optional
    mode : str
        used to set default parameter for peak picking.

    Methods
    -------
    find_peaks() : perform peak detection on the chromatograms.
    get_peak_params() : convert peak information into a DataFrame.
    plot() : plot the chromatogram.

    See Also
    --------

    """

    def __init__(self, spint: np.ndarray, rt: np.ndarray,
                 mz: Optional[float] = None, start: Optional[int] = None,
                 end: Optional[int] = None, mode: Optional[str] = None):
        """
        Constructor of the Chromatogram.

        Parameters
        ----------
        spint : array of non negative numbers.
            Intensity values of each scan
        rt : array of positive numbers.
            Retention time values.
        mz : positive number, optional
            m/z value used to generate the chromatogram
        start : int, optional
        end : int, optional
        mode : {"uplc", "hplc"}, optional
            used to set default parameters in peak picking. If None, `mode` is
            set to uplc.
        """
        if mode is None:
            self.mode = "uplc"
        elif mode in ["uplc", "hplc"]:
            self.mode = mode
        else:
            msg = "mode must be None, uplc or hplc"
            raise ValueError(msg)

        self.rt = rt
        self.spint = spint
        self.mz = mz
        self.peaks = None

        if start is None:
            self.start = 0
        if end is None:
            self.end = rt.size

    def find_peaks(self, cwt_params: Optional[dict] = None) -> None:
        """
        Find peaks with the modified version of the cwt algorithm described in
        the CentWave algorithm [1]_. Peaks are added to the peaks
        attribute of the Chromatogram object.

        Parameters
        ----------
        cwt_params: dict
            key-value parameters to overwrite the defaults in the pick_cwt
            function. The default are obtained using the mode attribute.

        See Also
        --------
        pick_cwt : peak detection using the CWT algorithm.
        get_lc_cwt_params : set default parameters for pick_cwt.

        References
        ----------
        ..  [1] Tautenhahn, R., Böttcher, C. & Neumann, S. Highly sensitive
            feature detection for high resolution LC/MS. BMC Bioinformatics 9,
            504 (2008). https://doi.org/10.1186/1471-2105-9-504

        """
        default_params = get_lc_cwt_params(self.mode)

        if cwt_params:
            default_params.update(cwt_params)

        widths = make_widths_lc(self.mode)
        peak_list = peaks.pick_cwt(self.rt[self.start:self.end],
                                   self.spint[self.start:self.end],
                                   widths, **default_params)
        self.peaks = peak_list
        if self.start > 0:
            for peak in self.peaks:
                peak.start += self.start
                peak.end += self.start
                peak.loc += self.start

    def get_peak_params(self, subtract_bl: bool = True,
                        rt_estimation: str = "weighted") -> pd.DataFrame:
        """
        Compute peak parameters using retention time and mass-to-charge ratio

        Parameters
        ----------
        subtract_bl: bool
            If True subtracts the estimated baseline from the intensity and
            area.
        rt_estimation: {"weighted", "apex"}
            if "weighted", the peak retention time is computed as the weighted
            mean of rt in the extension of the peak. If "apex", rt is
            simply the value obtained after peak picking.

        Returns
        -------
        peak_params: DataFrame
        """
        if self.peaks is None:
            msg = "`pick_cwt` method must be runned before using this method"
            raise ValueError(msg)

        peak_params = list()
        for peak in self.peaks:
            tmp = peak.get_peak_params(self.spint, x=self.rt,
                                       subtract_bl=subtract_bl,
                                       center_estimation=rt_estimation)
            tmp["rt"] = tmp.pop("location")
            # if isinstance(self.mz, np.ndarray):
            #     missing = np.isnan(self.mz[peak.start:peak.end])
            #     mz_not_missing = self.mz[peak.start:peak.end][~missing]
            #     sp_not_missing = self.spint[peak.start:peak.end][~missing]
            #     mz_mean = np.average(mz_not_missing, weights=sp_not_missing)
            #     mz_std = mz_not_missing.std()
            #     tmp["mz mean"] = mz_mean
            #     tmp["mz std"] = mz_std
            # else:
            tmp["mz mean"] = self.mz
            peak_params.append(tmp)
        peak_params = pd.DataFrame(data=peak_params)
        if not peak_params.empty:
            peak_params = peak_params.sort_values("rt").reset_index(drop=True)
        return peak_params

    def plot(self, subtract_bl: bool = True, draw: bool = True,
             fig_params: Optional[dict] = None,
             line_params: Optional[dict] = None,
             scatter_params: Optional[dict] = None) -> bokeh.plotting.Figure:
        """
        Plot the chromatogram.

        Parameters
        ----------
        subtract_bl : bool, optional
        draw : bool, optional
            if True run bokeh show function.
        fig_params : dict
            key-value parameters to pass into bokeh figure function.
        line_params : dict
            key-value parameters to pass into bokeh line function.
        scatter_params : dict
            key-value parameters to pass into bokeh line function.

        Returns
        -------
        bokeh Figure
        """
        # TODO: remove subtract_bl and other parameters...

        default_line_params = {"line_width": 1, "line_color": "black",
                               "alpha": 0.8}
        cmap = Set3[12] + Set3[12]

        if line_params is None:
            line_params = default_line_params
        else:
            for params in line_params:
                default_line_params[params] = line_params[params]
            line_params = default_line_params

        if fig_params is None:
            fig_params = dict()

        if scatter_params is None:
            scatter_params = dict()

        fig = bokeh.plotting.figure(**fig_params)
        fig.line(self.rt, self.spint, **line_params)
        if self.peaks:
            source = ColumnDataSource(
                self.get_peak_params(subtract_bl=subtract_bl))
            for k, peak in enumerate(self.peaks):
                fig.varea(self.rt[peak.start:(peak.end + 1)],
                          self.spint[peak.start:(peak.end + 1)], 0,
                          fill_alpha=0.8, fill_color=cmap[k])
            scatter = fig.scatter(source=source, x="rt", y="intensity",
                                  **scatter_params)
            # add hover tool only on scatter points
            tooltips = [("rt", "@rt"), ("mz", "@{mz mean}"),
                        ("intensity", "@intensity"),
                        ("area", "@area"), ("width", "@width")]
            hover = HoverTool(renderers=[scatter], tooltips=tooltips)
            fig.add_tools(hover)

        if draw:
            bokeh.plotting.show(fig)
        return fig


class MSSpectrum:
    """
    Representation of a Mass Spectrum. Manages peak picking, isotopic
    distribution analysis and plotting of MS data.

    Attributes
    ----------
    mz : array of m/z values
    spint : array of intensity values.
    mode : str
        MS instrument type. Used to set default values in peak picking.

    Methods
    -------
    find_peaks() : perform peak detection on the MS spectrum.
    get_peak_params() : convert peak information into a DataFrame.
    plot() : plot the MS spectrum.

    """
    def __init__(self, mz: np.ndarray, spint: np.ndarray,
                 mode: Optional[str] = None):
        """
        Constructor of the MSSpectrum.

        Parameters
        ----------
        mz: array
            m/z values.
        spint: array
            intensity values.

        """
        self.mz = mz
        self.spint = spint
        self.peaks = None

        if mode is None:
            self.mode = "qtof"
        elif mode in ["qtof", "orbitrap"]:
            self.mode = mode
        else:
            msg = "mode must be qtof or orbitrap"
            raise ValueError(msg)

    def find_peaks(self, mode: str = "qtof", cwt_params: Optional[dict] = None):
        """
        Find peaks with the modified version of the cwt algorithm described in
        the CentWave algorithm [1]_. Peaks are added to the peaks attribute.

        Parameters
        ----------
        cwt_params : dict
            key-value parameters to overwrite the defaults in the pick_cwt
            function from the peak module. Defaults are set using the `mode`
            attribute.

        See Also
        --------
        pick_cwt : peak detection using the CWT algorithm.
        get_ms_cwt_params : set default parameters for pick_cwt.

        References
        ----------
        ..  [1] Tautenhahn, R., Böttcher, C. & Neumann, S. Highly sensitive
            feature detection for high resolution LC/MS. BMC Bioinformatics 9,
            504 (2008). https://doi.org/10.1186/1471-2105-9-504

        """
        default_params = get_ms_cwt_params(mode)
        if cwt_params:
            default_params.update(cwt_params)

        widths = make_widths_ms(mode)
        peak_list = peaks.pick_cwt(self.mz, self.spint, widths,
                                   **default_params)
        self.peaks = peak_list

    def get_peak_params(self, subtract_bl: bool = True,
                        mz_estimation: str = "weighted") -> pd.DataFrame:
        """
        Compute peak parameters using mass-to-charge ratio and intensity

        Parameters
        ----------
        subtract_bl : bool
            If True subtracts the estimated baseline from the intensity and
            area.
        mz_estimation : {"weighted", "apex"}
            if "weighted", the location of the peak is computed as the weighted
            mean of x in the extension of the peak, using y as weights. If
            "apex", the location is simply the location obtained after peak
            picking.

        Returns
        -------
        peak_params: DataFrame

        """
        if self.peaks is None:
            msg = "`find_peaks` method must be used first."
            raise ValueError(msg)

        peak_params = [x.get_peak_params(self.spint, self.mz,
                                         subtract_bl=subtract_bl,
                                         center_estimation=mz_estimation)
                       for x in self.peaks]
        peak_params = pd.DataFrame(data=peak_params)
        peak_params.rename(columns={"location": "mz"}, inplace=True)
        if not peak_params.empty:
            peak_params = peak_params.sort_values("mz").reset_index(drop=True)
        return peak_params

    def plot(self, subtract_bl: bool = True, draw: bool = True,
             fig_params: Optional[dict] = None,
             line_params: Optional[dict] = None,
             scatter_params: Optional[dict] = None) -> bokeh.plotting.Figure:
        """
        Plot the MS spectrum.

        Parameters
        ----------
        subtract_bl : bool, optional
        draw : bool, optional
            if True run bokeh show function.
        fig_params : dict
            key-value parameters to pass into bokeh figure function.
        line_params : dict
            key-value parameters to pass into bokeh line function.
        scatter_params : dict
            key-value parameters to pass into bokeh line function.

        Returns
        -------
        bokeh Figure

        """

        default_line_params = {"line_width": 1, "line_color": "black",
                               "alpha": 0.8}
        cmap = Set3[12] + Set3[12] + Set3[12] + Set3[12]

        if line_params is None:
            line_params = default_line_params
        else:
            for params in line_params:
                default_line_params[params] = line_params[params]
            line_params = default_line_params

        if fig_params is None:
            fig_params = dict()

        if scatter_params is None:
            scatter_params = dict()

        fig = bokeh.plotting.figure(**fig_params)
        fig.line(self.mz, self.spint, **line_params)
        if self.peaks:
            source = \
                ColumnDataSource(self.get_peak_params(subtract_bl=subtract_bl))
            for k, peak in enumerate(self.peaks):
                fig.varea(self.mz[peak.start:(peak.end + 1)],
                          self.spint[peak.start:(peak.end + 1)], 0,
                          fill_alpha=0.8, fill_color=cmap[k])
            scatter = fig.scatter(source=source, x="mz", y="intensity",
                                  **scatter_params)
            # add hover tool only on scatter points
            tooltips = [("mz", "@{mz}{%0.4f}"),
                        ("intensity", "@intensity"),
                        ("area", "@area"), ("width", "@width")]
            hover = HoverTool(renderers=[scatter], tooltips=tooltips)
            hover.formatters = {"mz": "printf"}
            fig.add_tools(hover)

        if draw:
            bokeh.plotting.show(fig)
        return fig


TempRoi = namedtuple("TempRoi", ["mz", "sp", "scan"])


def make_empty_temp_roi():
    return TempRoi(mz=list(), sp=list(), scan=list())


class Roi(Chromatogram):
    """
    mz traces where a chromatographic peak may be found. Subclassed from
    Chromatogram. To be used with the detect_features method of MSData.

    Attributes
    ----------
    first_scan : int
        first scan of the raw data where the ROI was detected.
    """
    def __init__(self, spint: np.ndarray, mz: np.ndarray, rt: np.ndarray,
                 first_scan: int, mode: Optional[str]):
        super(Roi, self).__init__(spint, rt, mode=mode)
        self.mz = mz
        self.first_scan = first_scan

    def fill_nan(self):
        """
        fill missing intensity values using linear interpolation.
        """
        missing = np.isnan(self.spint)
        interpolator = interp1d(self.rt[~missing], self.spint[~missing])
        self.spint[missing] = interpolator(self.rt[missing])

    def get_mean_mz(self):
        missing = np.isnan(self.mz)
        return np.average(self.mz[~missing], weights=self.spint[~missing])

    def get_peak_params(self, subtract_bl: bool = True,
                        rt_estimation: str = "weighted") -> dict:
        """
        Compute peak parameters using retention time and mass-to-charge ratio

        Parameters
        ----------
        subtract_bl: bool
            If True subtracts the estimated baseline from the intensity and
            area.
        rt_estimation: {"weighted", "apex"}
            if "weighted", the peak retention time is computed as the weighted
            mean of rt in the extension of the peak. If "apex", rt is
            simply the value obtained after peak picking.

        Returns
        -------
        peak_params: pandas.DataFrame
        """
        if self.peaks is None:
            msg = "`pick_cwt` method must be runned before using this method"
            raise ValueError(msg)

        peak_params = {"rt": list(), "intensity": list(), "width": list(),
                       "area": list(), "mz": list(), "mz std": list()}
        for peak in self.peaks:
            tmp = peak.get_peak_params(self.spint, x=self.rt,
                                       subtract_bl=subtract_bl,
                                       center_estimation=rt_estimation)
            tmp["rt"] = tmp.pop("location")
            for k in peak_params:
                if k not in ["mz", "mz std"]:
                    peak_params[k].append(tmp[k])

            # set mz mean and mz std
            missing = np.isnan(self.mz[peak.start:peak.end])
            if ~missing.all():
                mz_not_missing = self.mz[peak.start:peak.end][~missing]
                sp_not_missing = self.spint[peak.start:peak.end][~missing]
                mz_mean = np.average(mz_not_missing, weights=sp_not_missing)
                mz_std = mz_not_missing.std()
            else:
                mz_mean = np.nan
                mz_std = np.nan
            peak_params["mz"].append(mz_mean)
            peak_params["mz std"].append(mz_std)
        return peak_params


class _RoiProcessor:
    """
    Class used by make_roi function to generate Roi instances.

    Attributes
    ----------
    mz_mean: numpy.ndarray
        mean value of mz for a given row in mz_array. Used to add new values
        based on a tolerance. its updated after adding a new column
    n_missing: numpy.ndarray
        number of consecutive missing values. Used to detect finished rois
    roi: list[ROI]
    """

    def __init__(self, mz_seed: np.ndarray, max_missing: int = 1,
                 min_length: int = 5, min_intensity: float = 0,
                 tolerance: float = 0.005, multiple_match: str = "closest",
                 mz_reduce: Union[str, Callable] = "mean",
                 sp_reduce: Union[str, Callable] = "sum",
                 mode: Optional[str] = None):
        """

        Parameters
        ----------
        mz_seed: numpy.ndarray
            initial values to build rois
        max_missing: int
            maximum number of missing consecutive values. when a row surpass
            this number the roi is flagged as finished.
        min_length: int
            The minimum length of a finished roi to be considered valid before
            being added to the roi list.
        min_intensity: float
        tolerance: float
            mz tolerance used to connect values.
        multiple_match: {"closest", "reduce"}
            how to match peaks when there is more than one match. If mode is
            `closest`, then the closest peak is assigned as a match and the
            others are assigned to no match. If mode is `reduce`, then a unique
            mz and intensity value is generated using the reduce function in
            `mz_reduce` and `spint_reduce` respectively.
        mz_reduce: str or callable
            function used to reduce mz values. Can be a function accepting
            numpy arrays and returning numbers. Only used when `multiple_match`
            is reduce. See the following prototype:

            def mz_reduce(mz_match: np.ndarray) -> float:
                pass

        sp_reduce: str or callable
            function used to reduce spint values. Can be a function accepting
            numpy arrays and returning numbers. Only used when `multiple_match`
            is reduce. To use custom functions see the prototype shown on
            `mz_reduce`.
        mode: str, optional
            Mode used to create ROI.
        """
        if len(mz_seed.shape) != 1:
            msg = "array must be a vector"
            raise ValueError(msg)

        if multiple_match not in ["closest", "reduce"]:
            msg = "Valid modes are closest or reduce"
            raise ValueError(msg)

        if mz_reduce == "mean":
            self._mz_reduce = np.mean
        else:
            self._mz_reduce = mz_reduce

        if sp_reduce == "mean":
            self._spint_reduce = np.mean
        elif sp_reduce == "sum":
            self._spint_reduce = np.sum
        else:
            self._spint_reduce = sp_reduce

        self.mz_mean = mz_seed.copy()
        self.roi_index = np.arange(mz_seed.size)
        self.n_missing = np.zeros_like(mz_seed, dtype=int)
        self.max_intensity = np.zeros_like(mz_seed)
        self.length = np.zeros_like(mz_seed, dtype=int)
        self.index = 0
        self.temp_roi_dict = {x: make_empty_temp_roi() for x in self.roi_index}
        self.roi = list()
        self.min_intensity = min_intensity
        self.max_missing = max_missing
        self.min_length = min_length
        self.tolerance = tolerance
        self.multiple_match = multiple_match
        self.mode = mode

    def add(self, mz: np.ndarray, sp: np.ndarray, targeted: bool = False):
        """
        Adds new mz and spint values to temporal roi.
        """

        # find matching values
        match_index, mz_match, sp_match, mz_no_match, sp_no_match = \
            _match_mz(self.mz_mean, mz, sp, self.tolerance,
                      self.multiple_match, self._mz_reduce, self._spint_reduce)

        for k, k_mz, k_sp in zip(match_index, mz_match, sp_match):
            k_temp_roi = self.temp_roi_dict[self.roi_index[k]]
            k_temp_roi.mz.append(k_mz)
            k_temp_roi.sp.append(k_sp)
            k_temp_roi.scan.append(self.index)

        # update mz_mean and missings
        updated_mean = ((self.mz_mean[match_index] * self.length[match_index]
                         + mz_match) / (self.length[match_index] + 1))

        self.length[match_index] += 1
        self.n_missing += 1
        self.n_missing[match_index] = 0
        self.max_intensity[match_index] = \
            np.maximum(self.max_intensity[match_index], sp_match)
        if not targeted:
            self.mz_mean[match_index] = updated_mean
            self.extend(mz_no_match, sp_no_match)
        self.index += 1

    def append_to_roi(self, rt: np.ndarray, targeted: bool = False):
        """
        Remove completed ROI. Valid ROI are appended toi roi attribute.
        """

        # check completed rois
        is_completed = self.n_missing > self.max_missing

        # the most common case are short rois that must be discarded
        is_valid_roi = ((self.length >= self.min_length) &
                        (self.max_intensity >= self.min_intensity))

        # add completed roi
        completed_index = np.where(is_completed)[0]
        for ind in completed_index:
            roi_ind = self.roi_index[ind]
            finished_roi = self.temp_roi_dict.pop(roi_ind)
            if is_valid_roi[ind]:
                roi = tmp_roi_to_roi(finished_roi, rt, mode=self.mode)
                self.roi.append(roi)
        if targeted:
            self.n_missing[is_completed] = 0
            self.length[is_completed] = 0
            self.max_intensity[is_completed] = 0
            max_roi_ind = self.roi_index.max()
            n_completed = is_completed.sum()
            new_indices = np.arange(max_roi_ind + 1,
                                    max_roi_ind + 1 + n_completed)
            self.roi_index[is_completed] = new_indices
            new_tmp_roi = {k: make_empty_temp_roi() for k in new_indices}
            self.temp_roi_dict.update(new_tmp_roi)
        else:
            self.mz_mean = self.mz_mean[~is_completed]
            self.n_missing = self.n_missing[~is_completed]
            self.length = self.length[~is_completed]
            self.roi_index = self.roi_index[~is_completed]
            self.max_intensity = self.max_intensity[~is_completed]

    def extend(self, mz: np.ndarray, sp: np.ndarray):
        """adds new mz values to mz_mean"""
        max_index = self.roi_index.max()
        new_indices = np.arange(mz.size) + max_index + 1
        mz_mean_tmp = np.hstack((self.mz_mean, mz))
        roi_index_tmp = np.hstack((self.roi_index, new_indices))
        sorted_index = np.argsort(mz_mean_tmp)
        n_missing_tmp = np.zeros_like(new_indices, dtype=int)
        n_missing_tmp = np.hstack((self.n_missing, n_missing_tmp))
        length_tmp = np.ones_like(new_indices, dtype=int)
        length_tmp = np.hstack((self.length, length_tmp))
        max_int_tmp = np.zeros_like(new_indices, dtype=float)
        max_int_tmp = np.hstack((self.max_intensity, max_int_tmp))

        for k_index, k_mz, k_sp in zip(new_indices, mz, sp):
            new_roi = TempRoi(mz=[k_mz], sp=[k_sp], scan=[self.index])
            self.temp_roi_dict[k_index] = new_roi
        self.mz_mean = mz_mean_tmp[sorted_index]
        self.roi_index = roi_index_tmp[sorted_index]
        self.n_missing = n_missing_tmp[sorted_index]
        self.length = length_tmp[sorted_index]
        self.max_intensity = max_int_tmp[sorted_index]

    def flag_as_completed(self):
        self.n_missing[:] = self.max_missing + 1


def _compare_max(old: np.ndarray, new: np.ndarray) -> np.ndarray:
    """
    returns the element-wise maximum between old and new

    Parameters
    ----------
    old: numpy.ndarray
    new: numpy.ndarray
        can have nan

    Returns
    -------
    numpy.ndarray
    """
    new[np.isnan(new)] = 0
    return np.maximum(old, new)


def _match_mz(mz1: np.ndarray, mz2: np.ndarray, sp2: np.ndarray,
              tolerance: float, mode: str, mz_reduce: Callable,
              sp_reduce: Callable):
    """
    aux function to add method in _RoiProcessor. Find matched values.

    Parameters
    ----------
    mz1: numpy.ndarray
        _RoiProcessor mz_mean
    mz2: numpy.ndarray
        mz values to match
    sp2: numpy.ndarray
        intensity values associated to mz2
    tolerance: float
        tolerance used to match values
    mode: {"closest", "merge"}
        Behaviour when more more than one peak in mz2 matches with a given peak
        in mz1. If mode is `closest`, then the closest peak is assigned as a
        match and the others are assigned to no match. If mode is `merge`, then
        a unique mz and int value is generated using the average of the mz and
        the sum of the intensities.

    Returns
    ------
    match_index: numpy.ndarray
        index when of peaks mathing in mz1.
    mz_match: numpy.ndarray
        values of mz2 that matches with mz1
    sp_match: numpy.ndarray
        values of sp2 that matches with mz1
    mz_no_match: numpy.ndarray
    sp_no_match: numpy.ndarray
    """
    closest_index = find_closest(mz1, mz2)
    dmz = np.abs(mz1[closest_index] - mz2)
    match_mask = (dmz <= tolerance)
    no_match_mask = ~match_mask
    match_index = closest_index[match_mask]

    # check multiple_matches
    unique, first_index, count_index = np.unique(match_index,
                                                 return_counts=True,
                                                 return_index=True)

    # set match values
    match_index = unique
    sp_match = sp2[match_mask][first_index]
    mz_match = mz2[match_mask][first_index]

    # compute matches for duplicates
    multiple_match_mask = count_index > 1
    first_index = first_index[multiple_match_mask]
    if first_index.size > 0:
        first_index_index = np.where(count_index > 1)[0]
        count_index = count_index[multiple_match_mask]
        iterator = zip(first_index_index, first_index, count_index)
        if mode == "closest":
            rm_index = list()   # list of duplicate index to remove
            mz_replace = list()
            spint_replace = list()
            for first_ind, index, count in iterator:
                # check which of the duplicate is closest, the rest are removed
                closest = \
                    np.argmin(dmz[match_mask][index:(index + count)]) + index
                mz_replace.append(mz2[match_mask][closest])
                spint_replace.append(sp2[match_mask][closest])
                remove = np.arange(index, index + count)
                remove = np.setdiff1d(remove, closest)
                rm_index.extend(remove)
            no_match_mask[rm_index] = True
            mz_match[first_index_index] = mz_replace
            sp_match[first_index_index] = spint_replace
        elif mode == "reduce":
            for first_ind, index, count in iterator:
                # check which of the duplicate is closest
                mz_multiple_match = mz2[match_mask][index:(index + count)]
                sp_multiple_match = sp2[match_mask][index:(index + count)]
                mz_match[first_ind] = mz_reduce(mz_multiple_match)
                sp_match[first_ind] = sp_reduce(sp_multiple_match)
        else:
            msg = "mode must be `closest` or `merge`"
            raise ValueError(msg)

    mz_no_match = mz2[no_match_mask]
    sp_no_match = sp2[no_match_mask]
    return match_index, mz_match, sp_match, mz_no_match, sp_no_match


def tmp_roi_to_roi(tmp_roi: TempRoi, rt: np.ndarray,
                   mode: Optional[str] = None) -> Roi:
    first_scan = tmp_roi.scan[0]
    last_scan = tmp_roi.scan[-1]
    size = last_scan + 1 - first_scan
    mz_tmp = np.ones(size) * np.nan
    spint_tmp = mz_tmp.copy()
    tmp_index = np.array(tmp_roi.scan) - tmp_roi.scan[0]
    rt_tmp = rt[first_scan:(last_scan + 1)]
    mz_tmp[tmp_index] = tmp_roi.mz
    spint_tmp[tmp_index] = tmp_roi.sp
    roi = Roi(spint_tmp, mz_tmp, rt_tmp, first_scan, mode=mode)
    return roi


def make_roi(msexp: msexperiment, tolerance: float, max_missing: int,
             min_length: int, min_intensity: float, multiple_match: str,
             targeted_mz: Optional[np.ndarray] = None,
             start: Optional[int] = None, end: Optional[int] = None,
             mz_reduce: Union[str, Callable] = "mean",
             sp_reduce: Union[str, Callable] = "sum",
             mode: Optional[str] = None
             ) -> List[Roi]:
    """
    Make Region of interest from MS data in centroid mode. [1]

    Parameters
    ----------
    max_missing: int
        maximum number of missing consecutive values. when a row surpass this
        number the roi is considered as finished and is added to the roi list if
        it meets the length and intensity criteria.
    min_length: int
        The minimum length of a roi to be considered valid.
    min_intensity: float
        Minimum intensity in a roi to be considered valid.
    tolerance: float
        mz tolerance to connect values across scans
    start: int, optional
        First scan to analyze. If None starts at scan 0
    end: int, optional
        Last scan to analyze. If None, uses the last scan number.
    multiple_match: {"closest", "reduce"}
        How to match peaks when there is more than one match. If mode is
        `closest`, then the closest peak is assigned as a match and the
        others are assigned to no match. If mode is `reduce`, then unique
        mz and intensity values are generated using the reduce function in
        `mz_reduce` and `spint_reduce` respectively.
    mz_reduce: "mean" or Callable
        function used to reduce mz values. Can be a function accepting
        numpy arrays and returning numbers. Only used when `multiple_match`
        is reduce. See the following prototype:

        .. codeblock: python

        def mz_reduce(mz_match: np.ndarray) -> float:
            pass

        TODO: change mean for None.
    sp_reduce: {"mean", "sum"} or Callable
        function used to reduce spint values. Can be a function accepting
        numpy arrays and returning numbers. Only used when `multiple_match`
        is reduce. To use custom functions see the prototype shown on
        `mz_reduce`.
    targeted_mz: numpy.ndarray, optional
        if a list of mz is provided, roi are searched only using this list.

    Returns
    -------
    roi: list[Roi]

    References
    ----------
    .. [1] Tautenhahn, R., Böttcher, C. & Neumann, S. Highly sensitive
        feature detection for high resolution LC/MS. BMC Bioinformatics 9,
        504 (2008). https://doi.org/10.1186/1471-2105-9-504
    """
    if start is None:
        start = 0

    if end is None:
        end = msexp.getNrSpectra()

    if targeted_mz is None:
        mz_seed, _ = msexp.getSpectrum(start).get_peaks()
        targeted = False
    else:
        mz_seed = targeted_mz
        targeted = True

    size = end - start
    rt = np.zeros(size)
    processor = _RoiProcessor(mz_seed, max_missing=max_missing,
                              min_length=min_length,
                              min_intensity=min_intensity, tolerance=tolerance,
                              multiple_match=multiple_match,
                              mz_reduce=mz_reduce, sp_reduce=sp_reduce,
                              mode=mode)
    for k_scan in range(start, end):
        sp = msexp.getSpectrum(k_scan)
        rt[k_scan - start] = sp.getRT()
        mz, spint = sp.get_peaks()
        processor.add(mz, spint, targeted=targeted)
        processor.append_to_roi(rt, targeted=targeted)
        assert (np.diff(processor.mz_mean) >= 0).all()
    # add roi not completed during last scan
    processor.flag_as_completed()
    processor.append_to_roi(rt)
    return processor.roi

def detect_roi_peaks(roi: List[Roi],
                     subtract_bl: bool = True, rt_estimation: str = "weighted",
                     cwt_params: Optional[dict] = None) -> pd.DataFrame:
    if cwt_params is None:
        cwt_params = dict()

    peak_params = {"rt": list(), "intensity": list(), "width": list(),
                   "area": list(), "mz": list(), "mz std": list(),
                   "roi index": list(), "peak index": list()}

    for roi_index, k_roi in enumerate(roi):
        k_roi.fill_nan()
        k_roi.find_peaks(cwt_params=cwt_params)
        temp_params = k_roi.get_peak_params(subtract_bl=subtract_bl,
                                            rt_estimation=rt_estimation)
        temp_params["roi index"] = [roi_index] * len(temp_params["rt"])
        peak_index = np.arange(len(k_roi.peaks))
        temp_params["peak index"] = peak_index
        for k in peak_params:
            peak_params[k].extend(temp_params[k])
    n_features = len(peak_params["rt"])
    max_ft_str_length = len(str(n_features))

    def ft_formatter(x):
        return "FT" + str(x + 1).rjust(max_ft_str_length, "0")

    index = [ft_formatter(x) for x in range(n_features)]
    peak_params = pd.DataFrame(data=peak_params, index=index)
    return peak_params

# TODO: feature detection should be implemented using a general function
#  called detect_features, and accept modes, such as uplc, hplc, di, etc...
