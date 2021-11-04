# This file is part of cp_pipe.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

__all__ = ['PairedVisitListTaskRunner', 'SingleVisitListTaskRunner',
           'NonexistentDatasetTaskDataIdContainer', 'parseCmdlineNumberString',
           'countMaskedPixels', 'checkExpLengthEqual', 'ddict2dict']

import re
import numpy as np
from scipy.optimize import leastsq
import numpy.polynomial.polynomial as poly
from scipy.stats import norm

import lsst.pipe.base as pipeBase
import lsst.ip.isr as ipIsr
from lsst.ip.isr import isrMock
import lsst.log
import lsst.afw.image

import galsim


def sigmaClipCorrection(nSigClip):
    """Correct measured sigma to account for clipping.

    If we clip our input data and then measure sigma, then the
    measured sigma is smaller than the true value because real
    points beyond the clip threshold have been removed.  This is a
    small (1.5% at nSigClip=3) effect when nSigClip >~ 3, but the
    default parameters for measure crosstalk use nSigClip=2.0.
    This causes the measured sigma to be about 15% smaller than
    real.  This formula corrects the issue, for the symmetric case
    (upper clip threshold equal to lower clip threshold).

    Parameters
    ----------
    nSigClip : `float`
        Number of sigma the measurement was clipped by.

    Returns
    -------
    scaleFactor : `float`
        Scale factor to increase the measured sigma by.
    """
    varFactor = 1.0 - (2 * nSigClip * norm.pdf(nSigClip)) / (norm.cdf(nSigClip) - norm.cdf(-nSigClip))
    return 1.0 / np.sqrt(varFactor)


def calculateWeightedReducedChi2(measured, model, weightsMeasured, nData, nParsModel):
    """Calculate weighted reduced chi2.

    Parameters
    ----------

    measured : `list`
        List with measured data.

    model : `list`
        List with modeled data.

    weightsMeasured : `list`
        List with weights for the measured data.

    nData : `int`
        Number of data points.

    nParsModel : `int`
        Number of parameters in the model.

    Returns
    -------

    redWeightedChi2 : `float`
        Reduced weighted chi2.
    """
    wRes = (measured - model)*weightsMeasured
    return ((wRes*wRes).sum())/(nData-nParsModel)


def makeMockFlats(expTime, gain=1.0, readNoiseElectrons=5, fluxElectrons=1000,
                  randomSeedFlat1=1984, randomSeedFlat2=666, powerLawBfParams=[],
                  expId1=0, expId2=1):
    """Create a pair or mock flats with isrMock.

    Parameters
    ----------
    expTime : `float`
        Exposure time of the flats.

    gain : `float`, optional
        Gain, in e/ADU.

    readNoiseElectrons : `float`, optional
        Read noise rms, in electrons.

    fluxElectrons : `float`, optional
        Flux of flats, in electrons per second.

    randomSeedFlat1 : `int`, optional
        Random seed for the normal distrubutions for the mean signal
        and noise (flat1).

    randomSeedFlat2 : `int`, optional
        Random seed for the normal distrubutions for the mean signal
        and noise (flat2).

    powerLawBfParams : `list`, optional
        Parameters for `galsim.cdmodel.PowerLawCD` to simulate the
        brightter-fatter effect.

    expId1 : `int`, optional
        Exposure ID for first flat.

    expId2 : `int`, optional
        Exposure ID for second flat.

    Returns
    -------

    flatExp1 : `lsst.afw.image.exposure.ExposureF`
        First exposure of flat field pair.

    flatExp2 : `lsst.afw.image.exposure.ExposureF`
        Second exposure of flat field pair.

    Notes
    -----
    The parameters of `galsim.cdmodel.PowerLawCD` are `n, r0, t0, rx,
    tx, r, t, alpha`. For more information about their meaning, see
    the Galsim documentation
    https://galsim-developers.github.io/GalSim/_build/html/_modules/galsim/cdmodel.html  # noqa: W505
    and Gruen+15 (1501.02802).

    Example: galsim.cdmodel.PowerLawCD(8, 1.1e-7, 1.1e-7, 1.0e-8,
                                       1.0e-8, 1.0e-9, 1.0e-9, 2.0)
    """
    flatFlux = fluxElectrons  # e/s
    flatMean = flatFlux*expTime  # e
    readNoise = readNoiseElectrons  # e

    mockImageConfig = isrMock.IsrMock.ConfigClass()

    mockImageConfig.flatDrop = 0.99999
    mockImageConfig.isTrimmed = True

    flatExp1 = isrMock.FlatMock(config=mockImageConfig).run()
    flatExp2 = flatExp1.clone()
    (shapeY, shapeX) = flatExp1.getDimensions()
    flatWidth = np.sqrt(flatMean)

    rng1 = np.random.RandomState(randomSeedFlat1)
    flatData1 = rng1.normal(flatMean, flatWidth, (shapeX, shapeY)) + rng1.normal(0.0, readNoise,
                                                                                 (shapeX, shapeY))
    rng2 = np.random.RandomState(randomSeedFlat2)
    flatData2 = rng2.normal(flatMean, flatWidth, (shapeX, shapeY)) + rng2.normal(0.0, readNoise,
                                                                                 (shapeX, shapeY))
    # Simulate BF with power law model in galsim
    if len(powerLawBfParams):
        if not len(powerLawBfParams) == 8:
            raise RuntimeError("Wrong number of parameters for `galsim.cdmodel.PowerLawCD`. "
                               f"Expected 8; passed {len(powerLawBfParams)}.")
        cd = galsim.cdmodel.PowerLawCD(*powerLawBfParams)
        tempFlatData1 = galsim.Image(flatData1)
        temp2FlatData1 = cd.applyForward(tempFlatData1)

        tempFlatData2 = galsim.Image(flatData2)
        temp2FlatData2 = cd.applyForward(tempFlatData2)

        flatExp1.image.array[:] = temp2FlatData1.array/gain   # ADU
        flatExp2.image.array[:] = temp2FlatData2.array/gain  # ADU
    else:
        flatExp1.image.array[:] = flatData1/gain   # ADU
        flatExp2.image.array[:] = flatData2/gain   # ADU

    visitInfoExp1 = lsst.afw.image.VisitInfo(exposureId=expId1, exposureTime=expTime)
    visitInfoExp2 = lsst.afw.image.VisitInfo(exposureId=expId2, exposureTime=expTime)

    flatExp1.info.id = expId1
    flatExp1.getInfo().setVisitInfo(visitInfoExp1)
    flatExp2.info.id = expId2
    flatExp2.getInfo().setVisitInfo(visitInfoExp2)

    return flatExp1, flatExp2


def countMaskedPixels(maskedIm, maskPlane):
    """Count the number of pixels in a given mask plane.

    Parameters
    ----------
    maskedIm : `~lsst.afw.image.MaskedImage`
        Masked image to examine.
    maskPlane : `str`
        Name of the mask plane to examine.

    Returns
    -------
    nPix : `int`
        Number of pixels in the requested mask plane.
    """
    maskBit = maskedIm.mask.getPlaneBitMask(maskPlane)
    nPix = np.where(np.bitwise_and(maskedIm.mask.array, maskBit))[0].flatten().size
    return nPix


class PairedVisitListTaskRunner(pipeBase.TaskRunner):
    """Subclass of TaskRunner for handling intrinsically paired visits.

    This transforms the processed arguments generated by the ArgumentParser
    into the arguments expected by tasks which take visit pairs for their
    run() methods.

    Such tasks' run() methods tend to take two arguments,
    one of which is the dataRef (as usual), and the other is the list
    of visit-pairs, in the form of a list of tuples.
    This list is supplied on the command line as documented,
    and this class parses that, and passes the parsed version
    to the run() method.

    See pipeBase.TaskRunner for more information.
    """

    @staticmethod
    def getTargetList(parsedCmd, **kwargs):
        """Parse the visit list and pass through explicitly."""
        visitPairs = []
        for visitStringPair in parsedCmd.visitPairs:
            visitStrings = visitStringPair.split(",")
            if len(visitStrings) != 2:
                raise RuntimeError("Found {} visits in {} instead of 2".format(len(visitStrings),
                                                                               visitStringPair))
            try:
                visits = [int(visit) for visit in visitStrings]
            except Exception:
                raise RuntimeError("Could not parse {} as two integer visit numbers".format(visitStringPair))
            visitPairs.append(visits)

        return pipeBase.TaskRunner.getTargetList(parsedCmd, visitPairs=visitPairs, **kwargs)


def parseCmdlineNumberString(inputString):
    """Parse command line numerical expression sytax and return as list of int

    Take an input of the form "'1..5:2^123..126'" as a string, and return
    a list of ints as [1, 3, 5, 123, 124, 125, 126]

    Parameters
    ----------
    inputString : `str`
        String to be parsed.

    Returns
    -------
    outList : `list` [`int`]
        List of integers identified in the string.
    """
    outList = []
    for subString in inputString.split("^"):
        mat = re.search(r"^(\d+)\.\.(\d+)(?::(\d+))?$", subString)
        if mat:
            v1 = int(mat.group(1))
            v2 = int(mat.group(2))
            v3 = mat.group(3)
            v3 = int(v3) if v3 else 1
            for v in range(v1, v2 + 1, v3):
                outList.append(int(v))
        else:
            outList.append(int(subString))
    return outList


class SingleVisitListTaskRunner(pipeBase.TaskRunner):
    """Subclass of TaskRunner for tasks requiring a list of visits per dataRef.

    This transforms the processed arguments generated by the ArgumentParser
    into the arguments expected by tasks which require a list of visits
    to be supplied for each dataRef, as is common in `lsst.cp.pipe` code.

    Such tasks' run() methods tend to take two arguments,
    one of which is the dataRef (as usual), and the other is the list
    of visits.
    This list is supplied on the command line as documented,
    and this class parses that, and passes the parsed version
    to the run() method.

    See `lsst.pipe.base.TaskRunner` for more information.
    """

    @staticmethod
    def getTargetList(parsedCmd, **kwargs):
        """Parse the visit list and pass through explicitly."""
        # if this has been pre-parsed and therefore doesn't have length of one
        # then something has gone wrong, so execution should stop here.
        assert len(parsedCmd.visitList) == 1, 'visitList parsing assumptions violated'
        visits = parseCmdlineNumberString(parsedCmd.visitList[0])

        return pipeBase.TaskRunner.getTargetList(parsedCmd, visitList=visits, **kwargs)


class NonexistentDatasetTaskDataIdContainer(pipeBase.DataIdContainer):
    """A DataIdContainer for the tasks for which the output does
    not yet exist."""

    def makeDataRefList(self, namespace):
        """Compute refList based on idList.

        This method must be defined as the dataset does not exist before this
        task is run.

        Parameters
        ----------
        namespace
            Results of parsing the command-line.

        Notes
        -----
        Not called if ``add_id_argument`` called
        with ``doMakeDataRefList=False``.
        Note that this is almost a copy-and-paste of the vanilla
        implementation, but without checking if the datasets already exist,
        as this task exists to make them.
        """
        if self.datasetType is None:
            raise RuntimeError("Must call setDatasetType first")
        butler = namespace.butler
        for dataId in self.idList:
            refList = list(butler.subset(datasetType=self.datasetType, level=self.level, dataId=dataId))
            # exclude nonexistent data
            # this is a recursive test, e.g. for the sake of "raw" data
            if not refList:
                namespace.log.warning("No data found for dataId=%s", dataId)
                continue
            self.refList += refList


def irlsFit(initialParams, dataX, dataY, function, weightsY=None, weightType='Cauchy'):
    """Iteratively reweighted least squares fit.

    This uses the `lsst.cp.pipe.utils.fitLeastSq`, but applies weights
    based on the Cauchy distribution by default.  Other weight options
    are implemented.  See e.g. Holland and Welsch, 1977,
    doi:10.1080/03610927708827533

    Parameters
    ----------
    initialParams : `list` [`float`]
        Starting parameters.
    dataX : `numpy.array`, (N,)
        Abscissa data.
    dataY : `numpy.array`, (N,)
        Ordinate data.
    function : callable
        Function to fit.
    weightsY : `numpy.array`, (N,)
        Weights to apply to the data.
    weightType : `str`, optional
        Type of weighting to use.  One of Cauchy, Anderson, bisquare,
        box, Welsch, Huber, logistic, or Fair.

    Returns
    -------
    polyFit : `list` [`float`]
        Final best fit parameters.
    polyFitErr : `list` [`float`]
        Final errors on fit parameters.
    chiSq : `float`
        Reduced chi squared.
    weightsY : `list` [`float`]
        Final weights used for each point.

    Raises
    ------
    RuntimeError :
        Raised if an unknown weightType string is passed.
    """
    if not weightsY:
        weightsY = np.ones_like(dataX)

    polyFit, polyFitErr, chiSq = fitLeastSq(initialParams, dataX, dataY, function, weightsY=weightsY)
    for iteration in range(10):
        resid = np.abs(dataY - function(polyFit, dataX)) / np.sqrt(dataY)
        if weightType == 'Cauchy':
            # Use Cauchy weighting.  This is a soft weight.
            # At [2, 3, 5, 10] sigma, weights are [.59, .39, .19, .05].
            Z = resid / 2.385
            weightsY = 1.0 / (1.0 + np.square(Z))
        elif weightType == 'Anderson':
            # Anderson+1972 weighting.  This is a hard weight.
            # At [2, 3, 5, 10] sigma, weights are [.67, .35, 0.0, 0.0].
            Z = resid / (1.339 * np.pi)
            weightsY = np.where(Z < 1.0, np.sinc(Z), 0.0)
        elif weightType == 'bisquare':
            # Beaton and Tukey (1974) biweight.  This is a hard weight.
            # At [2, 3, 5, 10] sigma, weights are [.81, .59, 0.0, 0.0].
            Z = resid / 4.685
            weightsY = np.where(Z < 1.0, 1.0 - np.square(Z), 0.0)
        elif weightType == 'box':
            # Hinich and Talwar (1975).  This is a hard weight.
            # At [2, 3, 5, 10] sigma, weights are [1.0, 0.0, 0.0, 0.0].
            weightsY = np.where(resid < 2.795, 1.0, 0.0)
        elif weightType == 'Welsch':
            # Dennis and Welsch (1976).  This is a hard weight.
            # At [2, 3, 5, 10] sigma, weights are [.64, .36, .06, 1e-5].
            Z = resid / 2.985
            weightsY = np.exp(-1.0 * np.square(Z))
        elif weightType == 'Huber':
            # Huber (1964) weighting.  This is a soft weight.
            # At [2, 3, 5, 10] sigma, weights are [.67, .45, .27, .13].
            Z = resid / 1.345
            weightsY = np.where(Z < 1.0, 1.0, 1 / Z)
        elif weightType == 'logistic':
            # Logistic weighting.  This is a soft weight.
            # At [2, 3, 5, 10] sigma, weights are [.56, .40, .24, .12].
            Z = resid / 1.205
            weightsY = np.tanh(Z) / Z
        elif weightType == 'Fair':
            # Fair (1974) weighting.  This is a soft weight.
            # At [2, 3, 5, 10] sigma, weights are [.41, .32, .22, .12].
            Z = resid / 1.4
            weightsY = (1.0 / (1.0 + (Z)))
        else:
            raise RuntimeError(f"Unknown weighting type: {weightType}")
        polyFit, polyFitErr, chiSq = fitLeastSq(initialParams, dataX, dataY, function, weightsY=weightsY)

    return polyFit, polyFitErr, chiSq, weightsY


def fitLeastSq(initialParams, dataX, dataY, function, weightsY=None):
    """Do a fit and estimate the parameter errors using using
    scipy.optimize.leastq.

    optimize.leastsq returns the fractional covariance matrix. To
    estimate the standard deviation of the fit parameters, multiply
    the entries of this matrix by the unweighted reduced chi squared
    and take the square root of the diagonal elements.

    Parameters
    ----------
    initialParams : `list` [`float`]
        initial values for fit parameters. For ptcFitType=POLYNOMIAL,
        its length determines the degree of the polynomial.

    dataX : `numpy.array`, (N,)
        Data in the abscissa axis.

    dataY : `numpy.array`, (N,)
        Data in the ordinate axis.

    function : callable object (function)
        Function to fit the data with.

    weightsY : `numpy.array`, (N,)
        Weights of the data in the ordinate axis.

    Return
    ------
    pFitSingleLeastSquares : `list` [`float`]
        List with fitted parameters.

    pErrSingleLeastSquares : `list` [`float`]
        List with errors for fitted parameters.

    reducedChiSqSingleLeastSquares : `float`
        Reduced chi squared, unweighted if weightsY is not provided.
    """
    if weightsY is None:
        weightsY = np.ones(len(dataX))

    def errFunc(p, x, y, weightsY=None):
        if weightsY is None:
            weightsY = np.ones(len(x))
        return (function(p, x) - y)*weightsY

    pFit, pCov, infoDict, errMessage, success = leastsq(errFunc, initialParams,
                                                        args=(dataX, dataY, weightsY), full_output=1,
                                                        epsfcn=0.0001)

    if (len(dataY) > len(initialParams)) and pCov is not None:
        reducedChiSq = calculateWeightedReducedChi2(dataY, function(pFit, dataX), weightsY, len(dataY),
                                                    len(initialParams))
        pCov *= reducedChiSq
    else:
        pCov = np.zeros((len(initialParams), len(initialParams)))
        pCov[:, :] = np.nan
        reducedChiSq = np.nan

    errorVec = []
    for i in range(len(pFit)):
        errorVec.append(np.fabs(pCov[i][i])**0.5)

    pFitSingleLeastSquares = pFit
    pErrSingleLeastSquares = np.array(errorVec)

    return pFitSingleLeastSquares, pErrSingleLeastSquares, reducedChiSq


def fitBootstrap(initialParams, dataX, dataY, function, weightsY=None, confidenceSigma=1.):
    """Do a fit using least squares and bootstrap to estimate parameter errors.

    The bootstrap error bars are calculated by fitting 100 random data sets.

    Parameters
    ----------
    initialParams : `list` [`float`]
        initial values for fit parameters. For ptcFitType=POLYNOMIAL,
        its length determines the degree of the polynomial.

    dataX : `numpy.array`, (N,)
        Data in the abscissa axis.

    dataY : `numpy.array`, (N,)
        Data in the ordinate axis.

    function : callable object (function)
        Function to fit the data with.

    weightsY : `numpy.array`, (N,), optional.
        Weights of the data in the ordinate axis.

    confidenceSigma : `float`, optional.
        Number of sigmas that determine confidence interval for the
        bootstrap errors.

    Return
    ------
    pFitBootstrap : `list` [`float`]
        List with fitted parameters.

    pErrBootstrap : `list` [`float`]
        List with errors for fitted parameters.

    reducedChiSqBootstrap : `float`
        Reduced chi squared, unweighted if weightsY is not provided.
    """
    if weightsY is None:
        weightsY = np.ones(len(dataX))

    def errFunc(p, x, y, weightsY):
        if weightsY is None:
            weightsY = np.ones(len(x))
        return (function(p, x) - y)*weightsY

    # Fit first time
    pFit, _ = leastsq(errFunc, initialParams, args=(dataX, dataY, weightsY), full_output=0)

    # Get the stdev of the residuals
    residuals = errFunc(pFit, dataX, dataY, weightsY)
    # 100 random data sets are generated and fitted
    pars = []
    for i in range(100):
        randomDelta = np.random.normal(0., np.fabs(residuals), len(dataY))
        randomDataY = dataY + randomDelta
        randomFit, _ = leastsq(errFunc, initialParams,
                               args=(dataX, randomDataY, weightsY), full_output=0)
        pars.append(randomFit)
    pars = np.array(pars)
    meanPfit = np.mean(pars, 0)

    # confidence interval for parameter estimates
    errPfit = confidenceSigma*np.std(pars, 0)
    pFitBootstrap = meanPfit
    pErrBootstrap = errPfit

    reducedChiSq = calculateWeightedReducedChi2(dataY, function(pFitBootstrap, dataX), weightsY, len(dataY),
                                                len(initialParams))
    return pFitBootstrap, pErrBootstrap, reducedChiSq


def funcPolynomial(pars, x):
    """Polynomial function definition
    Parameters
    ----------
    params : `list`
        Polynomial coefficients. Its length determines the polynomial order.

    x : `numpy.array`, (N,)
        Abscisa array.

    Returns
    -------
    y : `numpy.array`, (N,)
        Ordinate array after evaluating polynomial of order
        len(pars)-1 at `x`.
    """
    return poly.polyval(x, [*pars])


def funcAstier(pars, x):
    """Single brighter-fatter parameter model for PTC; Equation 16 of
    Astier+19.

    Parameters
    ----------
    params : `list`
        Parameters of the model: a00 (brightter-fatter), gain (e/ADU),
        and noise (e^2).

    x : `numpy.array`, (N,)
        Signal mu (ADU).

    Returns
    -------
    y : `numpy.array`, (N,)
        C_00 (variance) in ADU^2.
    """
    a00, gain, noise = pars
    return 0.5/(a00*gain*gain)*(np.exp(2*a00*x*gain)-1) + noise/(gain*gain)  # C_00


def arrangeFlatsByExpTime(exposureList, exposureIdList):
    """Arrange exposures by exposure time.

    Parameters
    ----------
    exposureList : `list` [`lsst.afw.image.ExposureF`]
        Input list of exposures.

    exposureIdList : `list` [`int`]
        List of exposure ids as obtained by dataId[`exposure`].

    Returns
    ------
    flatsAtExpTime : `dict` [`float`,
                      `list`[(`lsst.afw.image.ExposureF`, `int`)]]
        Dictionary that groups flat-field exposures (and their IDs) that have
        the same exposure time (seconds).
    """
    flatsAtExpTime = {}
    assert len(exposureList) == len(exposureIdList), "Different lengths for exp. list and exp. ID lists"
    for exp, expId in zip(exposureList, exposureIdList):
        expTime = exp.getInfo().getVisitInfo().getExposureTime()
        listAtExpTime = flatsAtExpTime.setdefault(expTime, [])
        listAtExpTime.append((exp, expId))

    return flatsAtExpTime


def arrangeFlatsByExpId(exposureList, exposureIdList):
    """Arrange exposures by exposure ID.

    There is no guarantee that this will properly group exposures, but
    allows a sequence of flats that have different illumination
    (despite having the same exposure time) to be processed.

    Parameters
    ----------
    exposureList : `list`[`lsst.afw.image.ExposureF`]
        Input list of exposures.

    exposureIdList : `list`[`int`]
        List of exposure ids as obtained by dataId[`exposure`].

    Returns
    ------
    flatsAtExpId : `dict` [`float`,
                   `list`[(`lsst.afw.image.ExposureF`, `int`)]]
        Dictionary that groups flat-field exposures (and their IDs)
        sequentially by their exposure id.

    Notes
    -----

    This algorithm sorts the input exposures by their exposure id, and
    then assigns each pair of exposures (exp_j, exp_{j+1}) to pair k,
    such that 2*k = j, where j is the python index of one of the
    exposures (starting from zero).  By checking for the IndexError
    while appending, we can ensure that there will only ever be fully
    populated pairs.
    """
    flatsAtExpId = {}
    assert len(exposureList) == len(exposureIdList), "Different lengths for exp. list and exp. ID lists"
    # Sort exposures by expIds, which are in the second list `exposureIdList`.
    sortedExposures = sorted(zip(exposureList, exposureIdList), key=lambda pair: pair[1])

    for jPair, expTuple in enumerate(sortedExposures):
        if (jPair + 1) % 2:
            kPair = jPair // 2
            listAtExpId = flatsAtExpId.setdefault(kPair, [])
            try:
                listAtExpId.append(expTuple)
                listAtExpId.append(sortedExposures[jPair + 1])
            except IndexError:
                pass

    return flatsAtExpId


def checkExpLengthEqual(exp1, exp2, v1=None, v2=None, raiseWithMessage=False):
    """Check the exposure lengths of two exposures are equal.

    Parameters
    ----------
    exp1 : `lsst.afw.image.Exposure`
        First exposure to check
    exp2 : `lsst.afw.image.Exposure`
        Second exposure to check
    v1 : `int` or `str`, optional
        First visit of the visit pair
    v2 : `int` or `str`, optional
        Second visit of the visit pair
    raiseWithMessage : `bool`
        If True, instead of returning a bool, raise a RuntimeError if
        exposure times are not equal, with a message about which
        visits mismatch if the information is available.

    Returns
    -------
    success : `bool`
        This is true if the exposures have equal exposure times.

    Raises
    ------
    RuntimeError
        Raised if the exposure lengths of the two exposures are not equal
    """
    expTime1 = exp1.getInfo().getVisitInfo().getExposureTime()
    expTime2 = exp2.getInfo().getVisitInfo().getExposureTime()
    if expTime1 != expTime2:
        if raiseWithMessage:
            msg = "Exposure lengths for visit pairs must be equal. " + \
                  "Found %s and %s" % (expTime1, expTime2)
            if v1 and v2:
                msg += " for visit pair %s, %s" % (v1, v2)
            raise RuntimeError(msg)
        else:
            return False
    return True


def validateIsrConfig(isrTask, mandatory=None, forbidden=None, desirable=None, undesirable=None,
                      checkTrim=True, logName=None):
    """Check that appropriate ISR settings have been selected for the task.

    Note that this checks that the task itself is configured correctly rather
    than checking a config.

    Parameters
    ----------
    isrTask : `lsst.ip.isr.IsrTask`
        The task whose config is to be validated

    mandatory : `iterable` [`str`]
        isr steps that must be set to True. Raises if False or missing

    forbidden : `iterable` [`str`]
        isr steps that must be set to False. Raises if True, warns if missing

    desirable : `iterable` [`str`]
        isr steps that should probably be set to True. Warns is False,
        info if missing

    undesirable : `iterable` [`str`]
        isr steps that should probably be set to False. Warns is True,
        info if missing

    checkTrim : `bool`
        Check to ensure the isrTask's assembly subtask is trimming the
        images.  This is a separate config as it is very ugly to do
        this within the normal configuration lists as it is an option
        of a sub task.

    Raises
    ------
    RuntimeError
        Raised if ``mandatory`` config parameters are False,
        or if ``forbidden`` parameters are True.

    TypeError
        Raised if parameter ``isrTask`` is an invalid type.

    Notes
    -----
    Logs warnings using an isrValidation logger for desirable/undesirable
    options that are of the wrong polarity or if keys are missing.
    """
    if not isinstance(isrTask, ipIsr.IsrTask):
        raise TypeError(f'Must supply an instance of lsst.ip.isr.IsrTask not {type(isrTask)}')

    configDict = isrTask.config.toDict()

    if logName and isinstance(logName, str):
        log = lsst.log.getLogger(logName)
    else:
        log = lsst.log.getLogger("isrValidation")

    if mandatory:
        for configParam in mandatory:
            if configParam not in configDict:
                raise RuntimeError(f"Mandatory parameter {configParam} not found in the isr configuration.")
            if configDict[configParam] is False:
                raise RuntimeError(f"Must set config.isr.{configParam} to True for this task.")

    if forbidden:
        for configParam in forbidden:
            if configParam not in configDict:
                log.warning(f"Failed to find forbidden key {configParam} in the isr config. The keys in the"
                            " forbidden list should each have an associated Field in IsrConfig:"
                            " check that there is not a typo in this case.")
                continue
            if configDict[configParam] is True:
                raise RuntimeError(f"Must set config.isr.{configParam} to False for this task.")

    if desirable:
        for configParam in desirable:
            if configParam not in configDict:
                log.info(f"Failed to find key {configParam} in the isr config. You probably want"
                         " to set the equivalent for your obs_package to True.")
                continue
            if configDict[configParam] is False:
                log.warning(f"Found config.isr.{configParam} set to False for this task."
                            " The cp_pipe Config recommends setting this to True.")
    if undesirable:
        for configParam in undesirable:
            if configParam not in configDict:
                log.info(f"Failed to find key {configParam} in the isr config. You probably want"
                         " to set the equivalent for your obs_package to False.")
                continue
            if configDict[configParam] is True:
                log.warning(f"Found config.isr.{configParam} set to True for this task."
                            " The cp_pipe Config recommends setting this to False.")

    if checkTrim:  # subtask setting, seems non-trivial to combine with above lists
        if not isrTask.assembleCcd.config.doTrim:
            raise RuntimeError("Must trim when assembling CCDs. Set config.isr.assembleCcd.doTrim to True")


def ddict2dict(d):
    """Convert nested default dictionaries to regular dictionaries.

    This is needed to prevent yaml persistence issues.

    Parameters
    ----------
    d : `defaultdict`
        A possibly nested set of `defaultdict`.

    Returns
    -------
    dict : `dict`
        A possibly nested set of `dict`.
    """
    for k, v in d.items():
        if isinstance(v, dict):
            d[k] = ddict2dict(v)
    return dict(d)
