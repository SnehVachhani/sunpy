from __future__ import absolute_import
from __future__ import division

import os.path
from datetime import datetime
from datetime import timedelta
from warnings import warn
import copy
import csv
import urllib

import numpy as np
import pandas.tseries.index
import sqlite3
from itertools import chain
from astropy.io import fits

from sunpy.time import parse_time
from sunpy import config
from sunpy.lightcurve import LYRALightCurve
from sunpy.util.net import check_download_file
from sunpy.time.timerange import TimeRange

RISE_FACTOR = 1.01
FALL_FACTOR = 0.5
NORM = 0.001  # mean daily minimum in LYRA Zr channel, Jan 2010 to mid 2014.

LYTAF_REMOTE_PATH = "http://proba2.oma.be/lyra/data/lytaf/"
LYTAF_PATH = config.get("downloads", "download_dir")

def make_lyra_flare_list(lyralightcurve, lytaf_path=LYTAF_PATH):
    """
    Returns a LYRA flare list based on an input LYRALightCurve.

    Parameters
    ----------
    lyralightcurve : LYRALightCurve object
        LYRALightCurve of time period during which flares should be found.

    lytaf_path : string
        directory path where LYRA annotation files are stored.
        Default: sunpy download directory obtained from
        sunpy.config("downloads", download_dir").

    Returns
    -------
    lyra_events : numpy recarray
        Contains following tags giving information on flares found.
        lyra_events["start_time"] : datetime object
            Flare start time.
        lyra_events["peak_time"] : datetime object
            Flare peak time.
        lyra_events["end_time"] : datetime object
            Flare end time.
        lyra_events["start_irrad"] : float
            Irradiance at flare start time.  Unit=[W/m**2]
        lyra_events["peak_irrad"] : float
            Irradiance at flare peak time.  Unit=[W/m**2]
        lyra_events["end_irrad"] : float
            Irradiance at flare end time.  Unit=[W/m**2]

    Examples
    --------
    >>> lyra_events = make_lyra_flare_list('2014-08-01', '2014-08-02')

    """
    # Convert to lightcurve time to datetime objects
    time = lyralc.data.index.to_pydatetime()
    irradiance = np.asanyarray(lyralc.data["CHANNEL4"])
    # Create LYRA event list
    lyra_events = find_lyra_flares(time, irradiance, lytaf_path=lytaf_path)
    # Return result
    return lyra_events

def find_lyra_flares(time, irradiance, lytaf_path=LYTAF_PATH):
    """
    Finds events in a times series satisfying LYRA event definitions.

    This function finds events/flares in an input time series which satisfy
    the LYRA event definitions and returns the start, peak and end times
    and channels.  The LYRA event definitions have been devised for the
    Zirconium channel (channel 4).  For more info, see Notes section of this
    docstring.

    Parameters
    ----------
    irradiance : ndarray/array-like convertible to float64, e.g. np.array, list
        Contains irradiance measurements
    time : ndarray/array-like of of datetime objects, e.g. np.array, list
        Contains measurement times corresponding to each element in
        irradiance.  Must be same length as irradiance.
    lytaf_path : string
        directory path where the LYRA annotation files are stored.

    Returns
    -------
    lyra_events : numpy recarray
        Contains the start, peak and end times and irradiance values for each
        event found.  The fields of the recarray are: 'start_time',
        'peak_time', 'end_time', 'start_irrad', 'peak_irrad', 'end_irrad'.

    Notes
    -----
    The LYRA event definitions have been devised for the Zirconium channel
    (channel 4).

    Start time definition:
    1) There must be 4 consecutive minutes when the gradient of the
    1-minute-averaged irradiance is positive.
    2) The irradiance in the 4th minute must be at least 1% greater than that
    in the first minute.
    3) The start time is then the earliest consecutive positive gradient
    before the time identified by crieteria 1) and 2).
    N.B. The time series is additively scaled so that the median is
    0.001 W/m^2 (The mean irradiance for LYRA channel 4 from the start of the
    mission to mid 2014).  Criteria 2) is applied to this scaled data, not the
    observed.  This helps reduce the bias of detections due to variability in
    the solar background irradiance.

    End time definition:
    1) The irradiance must fall to half-way between the peak and initial
    irradiances.
    2) The end time is  then the latest consecutive negative gradient after
    the time identifie by criterion 1).

    Artifacts:
    The following artifacts, as defined in the LYRA time annotation file,
    (see reference [1]) are removed from the time series:
    "UV occ.", "Offpoint", "LAR", "Calibration", "SAA", "Vis occ.",
    "Operational Anomaly", "Glitch", "ASIC reload", "Moon in LYRA", "Recovery".
    In some cases this may cause a flare start or end times to be recorded
    slightly differently to what the eye would intuitively identify in the
    full data.  It may also cause some flares not be detected at all.
    However, it also drastically cuts down on false detections.

    References
    ---------
    [1] http://proba2.oma.be/data/TARDIS

    Examples
    --------

    """
    # Ensure inputs are of correct type
    irradiance = np.asanyarray(irradiance, dtype="float64")
    time = _check_datetime(time)
    # Define recarray to store results
    lyra_events = np.empty((0,), dtype=[("start_time", object),
                                        ("peak_time", object),
                                        ("end_time", object),
                                        ("start_irrad", float),
                                        ("peak_irrad", float),
                                        ("end_irrad", float)])
    # object LYRA artifacts from timeseries
    clean_time, irradiance_list, artifact_status = remove_lyra_artifacts(
        time, [irradiance], artifacts=["UV occ.", "Offpoint", "LAR", "SAA",
                                       "Calibration", "Vis occ.", "Glitch",
                                       "ASIC reload", "Operational Anomaly",
                                       "Moon in LYRA", "Recovery"],
        return_artifacts=True, lytaf_path=lytaf_path)
    clean_irradiance = irradiance_list[0]
    artifacts_removed = artifact_status["removed"]
    # Perform subtraction so median irradiance of time series is at
    # average daily minimum from first 4 years of mission.
    clean_irradiance_scaled = \
      clean_irradiance - (np.median(clean_irradiance)-NORM)
    # Get derivative of irradiance wrt time
    time_timedelta = clean_time[1:-1]-clean_time[0:-2]
    dt = np.zeros(len(time_timedelta), dtype="float64")
    for i, t, in enumerate(time_timedelta):
        dt[i] = t.total_seconds()
    dfdt = np.gradient(clean_irradiance_scaled[0:-2], dt)
    # Get locations where derivative is positive
    pos_deriv = np.where(dfdt > 0)[0]
    neg_deriv = np.where(dfdt < 0)[0]
    pos_deriv0 = np.where(dfdt >= 0)[0]
    neg_deriv0 = np.where(dfdt <= 0)[0]
    # Find difference between each time point and the one 4
    # observations ahead.
    time_timedelta4 = clean_time[4:-1]-clean_time[0:-5]
    dt4 = np.zeros(len(time_timedelta4))
    for i, t, in enumerate(time_timedelta4):
        dt4[i] = t.total_seconds()
    # Find all possible flare start times.
    end_series = len(clean_irradiance_scaled)-1
    i = 0
    while i < len(pos_deriv)-4:
        # Start time criteria
        if (pos_deriv[i:i+4]-pos_deriv[i] == np.arange(4)).all() and \
          dt4[pos_deriv[i]] > 210 and dt4[pos_deriv[i]] < 270 and \
          clean_irradiance_scaled[pos_deriv[i+4]]/ \
          clean_irradiance_scaled[pos_deriv[i]] >= RISE_FACTOR:
            # Find start time which is defined as earliest continuous
            # increase in irradiance before the point found by the above
            # criteria.
            try:
                k = np.where(neg_deriv0 < pos_deriv[i])[0][-1]
                kk = np.where(pos_deriv > neg_deriv0[k])[0][0]
            except IndexError:
                kk = i
            start_index = pos_deriv[kk]
            # If artifact is at start of flare, set start time to
            # directly afterwards.
            artifact_check = np.logical_and(
                artifacts_removed["end_time"] > clean_time[start_index],
                artifacts_removed["end_time"] < clean_time[pos_deriv[kk+2]])
            if artifact_check.any() == True:
                artifact_at_start = artifacts_removed[artifact_check][-1]
                new_index = np.where(
                    clean_time[pos_deriv] > artifact_at_start["end_time"])
                start_index = pos_deriv[new_index[0][0]]
            # Next, find index of flare end time.
            # If flare has not ended, do not record it.
            try:
                jj = np.where(neg_deriv > start_index)[0][0]
            except IndexError:
                i = i+1
            else:
                j = neg_deriv[jj]
                end_condition = False
                while end_condition == False and j < end_series:
                    j = j+1
                    max_irradiance = max(clean_irradiance_scaled[start_index:j])
                    end_condition = clean_irradiance_scaled[j] <= max_irradiance - \
                      (max_irradiance-clean_irradiance_scaled[start_index])*FALL_FACTOR
                if j >= end_series:
                    i = i+1
                else:
                    try:
                        m = np.where(pos_deriv0 > j)[0][0]
                    except IndexError:
                        i = i+1
                    else:
                        end_index = pos_deriv0[m]-1
                        # If artifact is at end of flare, set end time
                        # to directly beforehand.
                        artifact_check = np.logical_and(
                            artifacts_removed["begin_time"] < clean_time[end_index],
                            artifacts_removed["begin_time"] > clean_time[end_index-2])
                        if artifact_check.any() == True:
                            artifact_at_end = artifacts_removed[artifact_check][0]
                            new_index = np.where(
                                clean_time < artifact_at_end["begin_time"])
                            end_index = new_index[0][-1]
                        # find index of peak time
                        peak_index = np.where(clean_irradiance_scaled == \
                            max(clean_irradiance_scaled[start_index:end_index]))
                        peak_index = peak_index[0][0]
                        # Record flare start, peak and end times
                        lyra_events = np.append(
                            lyra_events, np.empty(1, dtype=lyra_events.dtype))
                        lyra_events[-1]["start_time"] = clean_time[start_index]
                        lyra_events[-1]["peak_time"] = clean_time[peak_index]
                        lyra_events[-1]["end_time"] = clean_time[end_index]
                        lyra_events[-1]["start_irrad"] = clean_irradiance[start_index]
                        lyra_events[-1]["peak_irrad"] = clean_irradiance[peak_index]
                        lyra_events[-1]["end_irrad"] = clean_irradiance[end_index]
                        # If the most recently found flare is during the
                        # decay phase of another reset end time of
                        # previous flare to start time of this flare.
                        if len(lyra_events) > 1 and \
                          lyra_events[-2]["end_time"] > lyra_events[-1]["start_time"]:
                            lyra_events[-2]["end_time"] = lyra_events[-1]["start_time"]
                            lyra_events[-2]["end_irrad"] = lyra_events[-1]["start_irrad"]
                        # Finally, set principle iterator, i, to the
                        # peak of the flare just found so that algorithm
                        # will start looking for flares during the decay
                        # phase of this flare and beyond.  This ensures
                        # that flares during the decay phase are also
                        # located.
                        i = np.where(pos_deriv > peak_index)[0][0]
        else:
            i = i+1

    return lyra_events

def remove_lyra_artifacts(time, channels=None, artifacts="All",
                          return_artifacts=False, fitsfile=None,
                          csvfile=None, filecolumns=None,
                          lytaf_path=LYTAF_PATH):
    """
    Removes periods of LYRA artifacts from a time series.

    This functions removes periods corresponding to certain artifacts recorded
    in the LYRA annotation file from an array of times given by the time input.
    If a list of arrays of other properties is supplied through the channels
    kwarg, then the relevant values from these arrays are also removed.  This
    is done by assuming that each element in each array supplied corresponds to
    the time in the same index in time array.  The artifacts to be removed are
    given via the artifacts kwarg.  The default is "all", meaning that all
    artifacts will be removed.  However, a subset of artifacts can be removed
    by supplying a list of strings of the desired artifact types.

    Parameters
    ----------
    time : ndarray/array-like of datetime objects
        Gives the times of the timeseries.

    channels : (optional) list of ndarrays/array-likes convertible to float64.
        Contains arrays of the irradiances taken at the times in the time
        variable.  Each element in the list must have the same number of
        elements as time.

    artifacts : list of strings
        Contain the artifact types to be removed.  For list of artifact types
        see reference [1].  For example, if user wants to remove only large
        angle rotations, listed at reference [1] as LAR, let artifacts=["LAR"].
        Default='All', i.e. all artifacts will be removed.

    return_artifacts : (optional) bool
        Set to True to return a numpy recarray containing the start time, end
        time and type of all artifacts removed.
        Default=False

    fitsfile : (optional) string
        file name (including file path and suffix, .fits) of output fits file
        which is generated if this kwarg is not None.
        Default=None, i.e. no fits file is output.

    csvfile : (optional) string
        file name (including file path and suffix, .csv) of output csv file
        which is generated if this kwarg is not None.
        Default=None, i.e. no csv file is output.

    filecolumns : (optional) list of strings
        Gives names of columns of any output files produced.  Although
        initially set to None above, the default is in fact
        ["time", "flux0", "flux1",..."fluxN"]
        where N is the number of irradiance arrays in the channels input
        (assuming 0-indexed counting).

    lytaf_path : string
        directory path where the LYRA annotation files are stored.

    Returns
    -------
    clean_time : ndarray/array-like of datetime objects
        time array with artifact periods removed.

    clean_channels : (optional) list ndarrays/array-likes convertible to float64
        list of irradiance arrays with artifact periods removed.

    artifact_status : (optional) dictionary
        List of 4 variables containing information on what artifacts were
        found, removed, etc. from the time series.
        artifact_status["lytaf"] = artifacts found : numpy recarray
            The full LYRA annotation file for the time series time range
            output by get_lytaf_events().
        artifact_status["removed"] = artifacts removed : numpy recarray
            Artifacts which were found and removed from from time series.
        artifact_status["not_removed"] = artifacts found but not removed :
              numpy recarray
            Artifacts which were found but not removed as they were not
            included when user defined artifacts kwarg.
        artifact_status["not_found"] = artifacts not found : list of strings
            Artifacts listed to be removed by user when defining artifacts
            kwarg which were not found in time series time range.

    References
    ----------
    [1] http://proba2.oma.be/data/TARDIS

    Example
    -------

    """
    # Check inputs
    time = _check_datetime(time)
    if not all(isinstance(artifact_type, str) for artifact_type in artifacts):
        raise TypeError("All elements in artifacts must in strings.")
    if type(channels) is not None and type(channels) is not list:
        raise TypeError("channels must be None or a list of numpy arrays of "
                        "dtype 'float64'.")
    # Define outputs
    clean_time = copy.deepcopy(time)
    clean_channels = copy.deepcopy(channels)
    artifacts_not_found = []
    # Get LYTAF file for given time range
    lytaf = get_lytaf_events(time[0], time[-1], lytaf_path=lytaf_path)

    # Find events in lytaf which are to be removed from time series.
    if artifacts == "All":
        artifact_indices = np.arange(len(lytaf["begin_time"]))
    else:
        artifact_indices = np.empty(0, dtype="int64")
        for artifact_type in artifacts:
            indices = np.where(lytaf["event_type"] == artifact_type)[0]
            # If none of a given type of artifact is found, record this
            # type in artifact_not_found list.
            if len(indices) == 0:
                artifacts_not_found.append(artifact_type)
            else:
                # Else, record the indices of the artifacts of this type
                artifact_indices = np.concatenate((artifact_indices, indices))
        artifact_indices.sort()

    # Remove relevant artifacts from timeseries. If none of the
    # artifacts the user wanted removed were found, raise a warning and
    # continue with code.
    if len(artifact_indices) == 0:
        warn("None of user supplied artifacts were found.")
        artifacts_not_found = artifacts
    else:
        # Remove periods corresponding to artifacts from flux and time
        # arrays.
        bad_indices = np.empty(0, dtype="int64")
        all_indices = np.arange(len(time))
        for index in artifact_indices:
            bad_period = np.logical_and(time >= lytaf["begin_time"][index],
                                        time <= lytaf["end_time"][index])
            bad_indices = np.append(bad_indices, all_indices[bad_period])
        clean_time = np.delete(time, bad_indices)
        if channels is not None:
            for i, f in enumerate(clean_channels):
                clean_channels[i] = np.delete(f, bad_indices)
    # If return_artifacts kwarg is True, return a list containing
    # information on what artifacts found, removed, etc.  See docstring.
    if return_artifacts is True:
        if artifacts_not_found == artifacts:
            artifact_status = {"lytaf": lytaf,
                               "removed": lytaf[artifact_indices],
                               "not_removed": None,
                               "not_found": artifacts_not_found}
        else:
            artifacts_removed = lytaf[artifact_indices]
            artifacts_not_removed = np.delete(lytaf, artifact_indices)
            if artifacts == "All":
                artifacts_not_found = None
            artifact_status = {"lytaf": lytaf, "removed": artifacts_removed,
                               "not_removed": artifacts_not_removed,
                               "not_found": artifacts_not_found}
    # Output FITS file if fits kwarg is set
    if fitsfile != None:
        # Create time array of time strings rather than datetime objects
        # and verify filecolumns have been correctly input.  If None,
        # generate generic filecolumns (see docstring og function called
        # below.
        string_time, filecolumns = _prep_columns(time, channels, filecolumns)
        # Prepare column objects.
        cols = [fits.Column(name=filecolumns[0], format="26A",
                            array=string_time)]
        if channels != None:
            for i, f in enumerate(channels):
                cols.append(fits.Column(name=filecolumns[i+1], format="D",
                                        array=f))
        coldefs = fits.ColDefs(cols)
        tbhdu = fits.new_table(coldefs)
        hdu = fits.PrimaryHDU()
        tbhdulist = fits.HDUList([hdu, tbhdu])
        # Write data to fits file.
        tbhdulist.writeto(fitsfile)
    # Output csv file if fits kwarg is set.
    if csvfile != None:
        # Create time array of time strings rather than datetime objects
        # and verify filecolumns have been correctly input.  If None,
        # generate generic filecolumns (see docstring og function called
        # below.
        string_time, filecolumns = _prep_columns(time, channels, filecolumns)
        # Open and write data to csv file.
        with open(csvfile, 'w') as openfile:
            csvwriter = csv.writer(openfile, delimiter=';')
            # Write header.
            csvwriter.writerow(filecolumns)
            # Write data.
            if channels == None:
                for i in range(len(time)):
                    csvwriter.writerow(string_time[i])
            else:
                for i in range(len(time)):
                    row = [string_time[i]]
                    for f in channels:
                        row.append(f[i])
                    csvwriter.writerow(row)
    # Return values.
    if return_artifacts is True:
        if channels is None:
            return clean_time, artifact_status
        else:
            return clean_time, clean_channels, artifact_status
    else:
        if channels is None:
            return clean_time
        else:
            return clean_time, clean_channels

def get_lytaf_events(start_time, end_time, lytaf_path=LYTAF_PATH,
                     combine_files=["lyra", "manual", "ppt", "science"],
                     csvfile=None):
    """
    Extracts combined lytaf file for given time range.

    Given a time range defined by start_time and end_time, this function
    extracts the segments of each LYRA annotation file and combines them.

    Parameters
    ----------
    start_time : datetime object or string
        Start time of period for which annotation file is required.
    end_time : datetime object or string
        End time of period for which annotation file is required.
    lytaf_path : string
        directory path where the LYRA annotation files are stored.
    combine_files : (optional) list of strings
        States which LYRA annotation files are to be combined.
        Default is all four, i.e. lyra, manual, ppt, science.
        See Notes section for an explanation of each.

    Returns
    -------
    lytaf : numpy record array containing the various parameters stored
        in the LYTAF files.

    Notes
    -----
    There are four LYRA annotation files which mark different types of events
    or artifacts in the data.  They are named annotation_suffix.db where
    suffix is a variable equalling either lyra, manual, ppt, or science.
    annotation_lyra.db : contains entries regarding possible effects to
        the data due to normal operation of LYRA instrument.
    annotation_manual.db : contains entries regarding possible effects
        to the data due to unusual or manually logged events.
    annotation_ppt.db : contains entries regarding possible effects to
        the data due to pointing or positioning of PROBA2.
    annotation_science.db : contains events in the data scientifically
        interesting, e.g. GOES flares.

    References
    ----------
    Further documentation: http://proba2.oma.be/data/TARDIS

    Examples
    --------

    """
    # Check inputs
    # Check start_time is a date string or datetime object
    if type(start_time) is str:
        start_time = parse_time(start_time)
    if type(start_time) is not datetime:
        raise TypeError("start_time must be a date string or datetime object")
    # Check start_time is a date string or datetime object
    if type(end_time) is str:
        end_time = parse_time(end_time)
    if type(end_time) is not datetime:
        raise TypeError("end_time must be a date string or datetime object")
    # Check combine_files contains correct inputs
    if not all(suffix in ["lyra", "manual", "ppt", "science"]
               for suffix in combine_files):
        raise TypeError("Elements in combine_files must be strings equalling "
                        "'lyra', 'manual', 'ppt', or 'science'.")
    # Remove any duplicates from combine_files input
    combine_files = list(set(combine_files))
    combine_files.sort()
    # Convert input times to UNIX timestamp format since this is the
    # time format in the annotation files
    start_time_uts = (start_time - datetime(1970, 1, 1)).total_seconds()
    end_time_uts = (end_time - datetime(1970, 1, 1)).total_seconds()

    # Define numpy record array which will hold the information from
    # the annotation file.
    lytaf = np.empty((0,), dtype=[("insertion_time", object),
                                  ("begin_time", object),
                                  ("reference_time", object),
                                  ("end_time", object),
                                  ("event_type", object),
                                  ("event_definition", object)])
    # Access annotation files
    for i, suffix in enumerate(combine_files):
        # Check database files are present
        dbname = "annotation_{0}.db".format(suffix)
        check_download_file(dbname, LYTAF_REMOTE_PATH, lytaf_path)
        # Open SQLITE3 annotation files
        connection = sqlite3.connect(os.path.join(lytaf_path, dbname))
        # Create cursor to manipulate data in annotation file
        cursor = connection.cursor()
        # Check if lytaf file spans the start and end times defined by
        # user.  If not, download newest version.
        # First get start time of first event and end time of last
        # event in lytaf.
        cursor.execute("select begin_time from event order by begin_time asc "
                       "limit 1;")
        db_first_begin_time = cursor.fetchone()[0]
        db_first_begin_time = datetime.fromtimestamp(db_first_begin_time)
        cursor.execute("select end_time from event order by end_time desc "
                       "limit 1;")
        db_last_end_time = cursor.fetchone()[0]
        db_last_end_time = datetime.fromtimestamp(db_last_end_time)
        # If lytaf does not include entire input time range...
        if end_time > db_last_end_time or start_time < db_first_begin_time:
            # ...close lytaf file...
            cursor.close()
            connection.close()
            # ...Download latest lytaf file...
            check_download_file(dbname, LYTAF_REMOTE_PATH, lytaf_path,
                                replace=True)
            # ...and open new version of lytaf database.
            connection = sqlite3.connect(os.path.join(lytaf_path, dbname))
            cursor = connection.cursor()
        # Select and extract the data from event table within file within
        # given time range
        cursor.execute("select insertion_time, begin_time, reference_time, "
                       "end_time, eventType_id from event where end_time >= "
                       "{0} and begin_time <= "
                       "{1}".format(start_time_uts, end_time_uts))
        event_rows = cursor.fetchall()
        # Select and extract the event types from eventType table
        cursor.row_factory = sqlite3.Row
        cursor.execute("select * from eventType")
        eventType_rows = cursor.fetchall()
        eventType_id = []
        eventType_type = []
        eventType_definition = []
        for eventType_row in eventType_rows:
            eventType_id.append(eventType_row["id"])
            eventType_type.append(eventType_row["type"])
            eventType_definition.append(eventType_row["definition"])
        # Enter desired information into the lytaf numpy record array
        for event_row in event_rows:
            id_index = eventType_id.index(event_row[4])
            lytaf = np.append(
                lytaf, np.array((datetime.utcfromtimestamp(event_row[0]),
                                 datetime.utcfromtimestamp(event_row[1]),
                                 datetime.utcfromtimestamp(event_row[2]),
                                 datetime.utcfromtimestamp(event_row[3]),
                                 eventType_type[id_index],
                                 eventType_definition[id_index]),
                                 dtype=lytaf.dtype))
        # Close file
        cursor.close()
        connection.close()
    # Sort lytaf in ascending order of begin time
    np.recarray.sort(lytaf, order="begin_time")

    # If csvfile kwarg is set, write out lytaf to csv file
    if csvfile != None:
        # Open and write data to csv file.
        with open(csvfile, 'w') as openfile:
            csvwriter = csv.writer(openfile, delimiter=';')
            # Write header.
            csvwriter.writerow(lytaf.dtype.names)
            # Write data.
            for row in lytaf:
                new_row = []
                new_row.append(row[0].strftime("%Y-%m-%dT%H:%M:%S"))
                new_row.append(row[1].strftime("%Y-%m-%dT%H:%M:%S"))
                new_row.append(row[2].strftime("%Y-%m-%dT%H:%M:%S"))
                new_row.append(row[3].strftime("%Y-%m-%dT%H:%M:%S"))
                new_row.append(row[4])
                new_row.append(row[5])
                csvwriter.writerow(new_row)

    #return event_rows, eventType_rows
    return lytaf

def _check_datetime(time):
    """
    Checks or tries to convert input array to array of datetime objects.

    Returns input time array with elements as datetime objects or raises an
    TypeError if time not of valid format.  Input format can be anything
    convertible to datetime by datetime() function or any time string valid as
    an input to sunpy.time.parse_time().

    """
    if not all(isinstance(t, datetime) for t in time):
        if type(time) == pandas.tseries.index.DatetimeIndex:
            time = time.to_pydatetime()
        elif all(isinstance(t, str) for t in time):
            time = np.array([parse_time(t) for t in time])
        else:
            try:
                time = np.array([datetime(t) for t in time])
            except TypeError:
                raise TypeError("time must be an array-like of datetime "
                                "objects, valid time strings, or pandas "
                                "DatetimeIndexes.")
    return time

def _prep_columns(time, channels, filecolumns):
    """
    Checks and prepares data to be written out to a file.

    Firstly, this function converts the elements of time, whose entries are
    assumed to be datetime objects, to time strings.  Secondly, it checks
    whether the number of elements in an input list of columns names,
    filecolumns, is equal to the number of arrays in the list, channels.
    If not, a Value Error is raised.

    """
    # Convert time which contains datetime objects to time strings.
    string_time = np.array([t.strftime("%Y-%m-%dT%H:%M:%S.%f") for t in time])
    # Check all the elements of filenames are strings...
    if all(isinstance(column, str) for column in filecolumns) is False:
        raise TypeError("All elements in filecolumns must by strings.")
    # Check filecolumns have the same number of elements as there are
    # arrays in channels, plus 1 for a time array.  Otherwise raise a
    # ValueError.
    if channels != None:
        ncol = 1 + len(channels)
    else:
        ncol = 1
    if len(filecolumns) != ncol:
        raise ValueError("Number of elements in filecolumns must be equal to "
                         "the number of input data arrays, "
                         "i.e. time + channels.")

    return string_time, filecolumns

def split_series_using_lytaf(timearray, data, lar):
    """
    Proba-2 analysis code for splitting up LYRA timeseries around locations
    where LARs (and other data events) are observed.

    Inputs
    ------
    timearray - array of times understandable by SunPy parse_time function.
    data - data array corresponding to the given time array
    lar - list
        Events obtained from querying LYTAF database using
        lyra.get_lytaf_events().

    Output
    ------
    output : list of dictionaries
        Each dictionary contains a sub-series corresponding to an interval of
        'good data'.
    """
    #lar is a dictionary with tags:
    #'start_time'
    #'end_time'
    #'ref_time'
    #'roi_description'
    #'event_type_description'
    #'event_type_id'

    n = len(timearray)
    mask = np.ones(n)
    el = len(lar)

    #make the input time array a list of datetime objects
    datetime_array = []
    for tim in timearray:
        datetime_array.append(parse_time(tim))


    #scan through each entry retrieved from the LYTAF database
    for j in range(0, el):
        #want to mark all times with events as bad in the mask, i.e. = 0
        start_dt = lar[j]['start_time']
        end_dt = lar[j]['end_time']

        #find the start and end indices for each event
        start_ind = np.searchsorted(datetime_array, start_dt)
        end_ind = np.searchsorted(datetime_array, end_dt)

        #append the mask to mark event as 'bad'
        mask[start_ind:end_ind] = 0

    diffmask = np.diff(mask)
    tmp_discontinuity = np.where(diffmask != 0.)
    #disc contains the indices of mask where there are discontinuities
    disc = tmp_discontinuity[0]

    if len(disc) == 0:
        print 'No events found within time series interval. '\
          +'Returning original series.'
        return [{'subtimes':datetime_array, 'subdata':data}]

    #-1 in diffmask means went from good data to bad
    #+1 means went from bad data to good

    #want to get the data between a +1 and the next -1

    #if the first discontinuity is a -1 then the start of the series was good.
    if diffmask[disc[0]] == -1.0:
        #make sure we can always start from disc[0] below
        disc = np.insert(disc, 0, 0)

    split_series = []

    limit = len(disc)
    #now extract the good data regions and ignore the bad ones
    for h in range(0, limit, 2):

        if h == limit-1:
            #can't index h+1 here. Go to end of series
            subtimes = datetime_array[disc[h]:-1]
            subdata = data[disc[h]:-1]
            subseries = {'subtimes':subtimes, 'subdata':subdata}
            split_series.append(subseries)
        else:
            subtimes = datetime_array[disc[h]:disc[h+1]]
            subdata = data[disc[h]:disc[h+1]]
            subseries = {'subtimes':subtimes, 'subdata':subdata}
            split_series.append(subseries)

    return split_series

def _lytaf_event2string(integers):
    if type(integers) == int:
        integers = [integers]
    #else:
    #    n=len(integers)
    out = []

    for i in integers:
        if i == 1:
            out.append('LAR')
        if i == 2:
            out.append('N/A')
        if i == 3:
            out.append('UV occult.')
        if i == 4:
            out.append('Vis. occult.')
        if i == 5:
            out.append('Offpoint')
        if i == 6:
            out.append('SAA')
        if i == 7:
            out.append('Auroral zone')
        if i == 8:
            out.append('Moon in LYRA')
        if i == 9:
            out.append('Moon in SWAP')
        if i == 10:
            out.append('Venus in LYRA')
        if i == 11:
            out.append('Venus in SWAP')

    return out
