"""
Definition of the SqliteCaseReader.
"""
from __future__ import print_function, absolute_import

import re
import sqlite3
from collections import OrderedDict

from six import PY2, PY3, iteritems
from six.moves import range

# import numpy as np

# from openmdao.recorders.base_case_reader import BaseCaseReader
from openmdao.recorders.case_new import Case, PromotedToAbsoluteMap

from openmdao.utils.record_util import check_valid_sqlite3_db, json_to_np_array, convert_to_np_array
from openmdao.recorders.sqlite_recorder import blob_to_array, format_version
from openmdao.utils.write_outputs import write_outputs

# from pprint import pprint
# import json

if PY2:
    import cPickle as pickle
    from openmdao.utils.general_utils import json_loads_byteified as json_loads
elif PY3:
    import pickle
    from json import loads as json_loads


# Regular expression used for splitting iteration coordinates, removes separator and iter counts
_coord_split_re = re.compile('\|\\d+\|*')

# regular expression used to determine if a node in an iteration coordinate represents a system
_coord_system_re = re.compile('(_solve_nonlinear|_apply_nonlinear|_solve_linear|_apply_linear)$')


def CaseReader(filename, pre_load=True):
    """
    Return a CaseReader for the given file.

    Parameters
    ----------
    filename : str
        A path to the recorded file.  The file should have been recorded using
        either the SqliteRecorder or the HDF5Recorder.
    pre_load : bool
        If True, load all the data into memory during initialization.

    Returns
    -------
    reader : BaseCaseReader
        An instance of a SqliteCaseReader that is reading filename.
    """
    reader = SqliteCaseReader(filename, pre_load)
    return reader


class BaseCaseReader():
    """
    Base class of all CaseReader implementations.

    Attributes
    ----------
    format_version : int
        The version of the format assumed when loading the file.
    problem_metadata : dict
        Metadata about the problem, including the system hierachy and connections.
    system_metadata : dict
        Metadata about each system in the recorded model, including options and scaling factors.
    """

    def __init__(self, filename, pre_load=False):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The path to the file containing the recorded data.
        pre_load : bool
            If True, load all the data into memory during initialization.
        """
        self._format_version = None
        self.problem_metadata = {}
        self.system_metadata = {}

    def get_cases(self, source, recurse=True, flat=False):
        """
        Initialize.

        Parameters
        ----------
        source : {'problem', 'driver', iteration_coordinate}
            Identifies which cases to return. 'iteration_coordinate' can refer to
            a system or a solver hierarchy location. Defaults to 'problem'.
        recurse : bool, optional
            If True, will enable iterating over all successors in case hierarchy
            rather than just the direct children. Defaults to True.
        flat : bool
            If True, return a flat dictionary rather than a nested dictionary.

        Returns
        -------
        dict
            The cases identified by the source
        """
        pass

    def get_case(self, id, recurse=True):
        """
        Initialize.

        Parameters
        ----------
        id : str
            The unique identifier of the case to return.
        recurse : bool, optional
            If True, will enable iterating over all successors in case hierarchy
            rather than just the direct children. Defaults to True.

        Returns
        -------
        dict
            The case identified by the is
        """
        pass

    def list_sources(self):
        """
        List of all the different recording sources for which there is recorded data.

        Returns
        -------
        list
            One or more of: `problem`, `driver`, `<component hierarchy location>`,
            `<solver hierarchy location>`
        """
        pass

    def list_source_vars(self, source):
        """
        List of all the different recording sources for which there is recorded data.

        Parameters
        ----------
        source : {'problem', 'driver', iteration_coordinate}
            Identifies which cases to return. 'iteration_coordinate' can refer to
            a system or a solver hierarchy location. Defaults to 'problem'.

        Returns
        -------
        dict
            {'inputs':[list of keys], 'outputs':[list of keys]}. Does not recurse.
        """
        pass


class SqliteCaseReader(BaseCaseReader):
    """
    A CaseReader specific to files created with SqliteRecorder.

    Attributes
    ----------
    problem_metadata : dict
        Metadata about the problem, including the system hierachy and connections.
    system_metadata : dict
        Metadata about each system in the recorded model, including options and scaling factors.
    _format_version : int
        The version of the format assumed when loading the file.
    _solver_metadata : dict
        Metadata for all the solvers in the model, including their type and options
    _filename : str
        The path to the filename containing the recorded data.
    _solver_metadata :
    _abs2meta : dict
        Dictionary mapping variables to their metadata
    _abs2prom : {'input': dict, 'output': dict}
        Dictionary mapping absolute names to promoted names.
    _prom2abs : {'input': dict, 'output': dict}
        Dictionary mapping promoted names to absolute names.
    _output2meta : dict
        Dictionary mapping output variables to their metadata
    _input2meta : dict
        Dictionary mapping input variables to their metadata
    _driver_cases : DriverCases
        Helper object for accessing cases from the driver_iterations table.
    _deriv_cases : DerivCases
        Helper object for accessing cases from the driver_derivatives table.
    _system_cases : SystemCases
        Helper object for accessing cases from the system_iterations table.
    _solver_cases : SolverCases
        Helper object for accessing cases from the solver_iterations table.
    _problem_cases : ProblemCases
        Helper object for accessing cases from the problem_cases table.
    """

    def __init__(self, filename, pre_load=False):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The path to the filename containing the recorded data.
        pre_load : bool
            If True, load all the data into memory during initialization.
        """
        super(SqliteCaseReader, self).__init__(filename, pre_load)

        check_valid_sqlite3_db(filename)

        # initialize private attributes
        self._filename = filename
        self._solver_metadata = {}
        self._abs2prom = None
        self._prom2abs = None
        self._abs2meta = None
        self._output2meta = None
        self._input2meta = None

        # collect metadata from database
        with sqlite3.connect(filename) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()

            # collect data from the metadata table. this includes:
            #   format_version
            #   VOI metadata, which is added to problem_metadata
            #   var name maps and metadata for all vars, which are saved as private attributes
            self._collect_metadata(cur)

            # collect data from the driver_metadata table. this includes:
            #   model viewer data, which is added to problem_metadata
            self._collect_driver_metadata(cur)

            # collect data from the system_metadata table. this includes:
            #   component metadata and scaling factors for each system,
            #   which is added to system_metadata
            self._collect_system_metadata(cur)

            # collect data from the solver_metadata table. this includes:
            #   solver class and options for each solver, which is saved as a private attribute
            self._collect_solver_metadata(cur)

        con.close()

        # create maps to facilitate accessing variable metadata using absolute or promoted name
        self._output2meta = PromotedToAbsoluteMap(self._abs2meta, self._prom2abs, self._abs2prom, 1)
        self._input2meta = PromotedToAbsoluteMap(self._abs2meta, self._prom2abs, self._abs2prom, 0)

        # create helper objects for accessing cases from the four iteration tables and
        # the problem cases table
        self._driver_cases = DriverCases(filename, self._format_version,
                                         self._abs2prom, self._abs2meta, self._prom2abs,
                                         self.problem_metadata['variables'])
        self._system_cases = SystemCases(filename, self._format_version,
                                         self._abs2prom, self._abs2meta, self._prom2abs)
        self._solver_cases = SolverCases(filename, self._format_version,
                                         self._abs2prom, self._abs2meta, self._prom2abs)
        self._problem_cases = ProblemCases(filename, self._format_version,
                                           self._abs2prom, self._abs2meta, self._prom2abs)

        # if requested, load all the iteration data into memory
        if pre_load:
            self._load_cases()

    def _collect_metadata(self, cur):
        """
        Load data from the metadata table.

        Populates the `format_version` attribute and the `variables` data in
        the `problem_metadata` attribute of this CaseReader. Also saves the
        variable name maps and variable metadata to private attributes.

        Parameters
        ----------
        cur : sqlite3.Cursor
            Database cursor to use for reading the data.
        """
        cur.execute('select * from metadata')

        row = cur.fetchone()

        # get format_version
        self._format_version = version = row['format_version']

        if version not in range(1, format_version + 1):
            raise ValueError('SQliteCaseReader encountered an unhandled '
                             'format version: {0}'.format(self._format_version))

        # add metadata for VOIs (des vars, objective, constraints) to problem metadata
        if version >= 4:
            self.problem_metadata['variables'] = json_loads(row['var_settings'])
        else:
            self.problem_metadata['variables'] = None

        # get variable name maps and metadata for all variables
        if version >= 3:
            self._abs2prom = json_loads(row['abs2prom'])
            self._prom2abs = json_loads(row['prom2abs'])
            self._abs2meta = json_loads(row['abs2meta'])

            # need to convert bounds to numpy arrays
            for name, meta in iteritems(self._abs2meta):
                if 'lower' in meta:
                    meta['lower'] = convert_to_np_array(meta['lower'], name, meta['shape'])
                if 'upper' in meta:
                    meta['upper'] = convert_to_np_array(meta['upper'], name, meta['shape'])

        elif version in (1, 2):
            abs2prom = row['abs2prom']
            prom2abs = row['prom2abs']
            abs2meta = row['abs2meta']

            if PY2:
                self._abs2prom = pickle.loads(str(abs2prom))
                self._prom2abs = pickle.loads(str(prom2abs))
                self._abs2meta = pickle.loads(str(abs2meta))

            if PY3:
                try:
                    self._abs2prom = pickle.loads(abs2prom)
                    self._prom2abs = pickle.loads(prom2abs)
                    self._abs2meta = pickle.loads(abs2meta)
                except TypeError:
                    # Reading in a python 2 pickle recorded pre-OpenMDAO 2.4.
                    self._abs2prom = pickle.loads(abs2prom.encode())
                    self._prom2abs = pickle.loads(prom2abs.encode())
                    self._abs2meta = pickle.loads(abs2meta.encode())

    def _collect_driver_metadata(self, cur):
        """
        Load data from the driver_metadata table.

        Populates the `problem_metadata` attribute of this CaseReader.

        Parameters
        ----------
        cur : sqlite3.Cursor
            Database cursor to use for reading the data.
        """
        cur.execute("SELECT model_viewer_data FROM driver_metadata")
        row = cur.fetchone()

        if row is not None:
            if self._format_version >= 3:
                driver_metadata = json_loads(row[0])
            elif self._format_version in (1, 2):
                if PY2:
                    driver_metadata = pickle.loads(str(row[0]))
                if PY3:
                    driver_metadata = pickle.loads(row[0])

            self.problem_metadata.update(driver_metadata)

    def _collect_system_metadata(self, cur):
        """
        Load data from the system table.

        Populates the `system_metadata` attribute of this CaseReader.

        Parameters
        ----------
        cur : sqlite3.Cursor
            Database cursor to use for reading the data.
        """
        cur.execute("SELECT id, scaling_factors, component_metadata FROM system_metadata")
        for row in cur:
            id = row[0]
            self.system_metadata[id] = {}

            if PY2:
                self.system_metadata[id]['scaling_factors'] = pickle.loads(str(row[1]))
                self.system_metadata[id]['component_options'] = pickle.loads(str(row[2]))
            if PY3:
                self.system_metadata[id]['scaling_factors'] = pickle.loads(row[1])
                self.system_metadata[id]['component_options'] = pickle.loads(row[2])

    def _collect_solver_metadata(self, cur):
        """
        Load data from the solver_metadata table.

        Populates the `solver_metadata` attribute of this CaseReader.

        Parameters
        ----------
        cur : sqlite3.Cursor
            Database cursor to use for reading the data.
        """
        cur.execute("SELECT id, solver_options, solver_class FROM solver_metadata")
        for row in cur:
            id = row[0]
            if PY2:
                solver_options = pickle.loads(str(row[1]))
            if PY3:
                solver_options = pickle.loads(row[1])
            solver_class = row[2]
            self._solver_metadata[id] = {
                'solver_options': solver_options,
                'solver_class': solver_class,
            }

    def _load_cases(self):
        """
        Load all driver, solver, and system cases into memory.
        """
        self._driver_cases._load_cases()
        self._solver_cases._load_cases()
        self._system_cases._load_cases()
        if self._format_version >= 2:
            self._problem_cases._load_cases()

    def list_sources(self):
        """
        List of all the different recording sources for which there is recorded data.

        Returns
        -------
        list
            One or more of: `problem`, `driver`, `<component hierarchy location>`,
            `<solver hierarchy location>`
        """
        sources = []

        if self._driver_cases.count() > 0:
            sources.extend(self._driver_cases.list_sources())
        if self._solver_cases.count() > 0:
            sources.extend(self._solver_cases.list_sources())
        if self._system_cases.count() > 0:
            sources.extend(self._system_cases.list_sources())
        if self._problem_cases.count() > 0:
            sources.extend(self._problem_cases.list_sources())

        return sources

    def list_cases(self, source='driver', recurse=False, flat=False):
        """
        Iterate over the driver and solver cases.

        Generator giving Driver and/or Solver cases in order.

        Parameters
        ----------
        source : {'problem', 'driver', iteration_coordinate}
            Identifies which cases to return. 'iteration_coordinate' can refer to
            a system or a solver hierarchy location. Defaults to 'driver'.
        recurse : bool, optional
            If True, will enable iterating over all successors in case hierarchy
            rather than just the direct children. Defaults to False.
        flat : bool, optional
            If False and there are child cases, then a nested ordered dictionary
            is returned rather than an iterator.
        """
        if not isinstance(source, str):
            raise TypeError("'source' parameter must be a string.")

        elif source == 'driver' and not recurse:
            return self._driver_cases.list_cases(recurse, flat)
        elif source == 'problem':
            return self._problem_cases.list_cases(recurse, flat)
        elif source in self._system_cases.list_sources():
            return self._system_cases.list_cases(source, recurse, flat)
        elif source in self._solver_cases.list_sources():
            return self._solver_cases.list_cases(source, recurse, flat)
        else:
            raise RuntimeError('Source not found:', source)

    def get_cases(self, source='driver', recurse=False, flat=False):
        """
        Iterate over the driver and solver cases.

        Generator giving Driver and/or Solver cases in order.

        Parameters
        ----------
        source : {'problem', 'driver', iteration_coordinate}
            Identifies which cases to return. 'iteration_coordinate' can refer to
            a system or a solver hierarchy location. Defaults to 'driver'.
        recurse : bool, optional
            If True, will enable iterating over all successors in case hierarchy
            rather than just the direct children. Defaults to False.
        flat : bool, optional
            If False and there are child cases, then a nested ordered dictionary
            is returned rather than an iterator.
        """
        if not isinstance(source, str):
            raise TypeError("'source' parameter must be a string.")

        elif source == 'problem':
            for case in self._problem_cases.cases():
                yield case

        elif source == 'driver' and not recurse:
            # return driver cases only
            for case in self._driver_cases.cases():
                yield case

        else:
            if source == 'driver':
                # return all driver cases and recurse to solver cases
                iter_coord = ''
            elif source.find('solve_nonlinear'):
                iter_coord = self._solver_cases._get_first(source).iteration_coordinate
                print('first case from solver', source, '=', iter_coord)
            else:
                iter_coord = self._driver_cases._get_first(source).iteration_coordinate
                print('first case from driver', source, '=', iter_coord)

            driver_iter = []
            solver_iter = []

            with sqlite3.connect(self._filename) as con:
                cur = con.cursor()
                cur.execute("SELECT iteration_coordinate FROM driver_iterations "
                            "ORDER BY counter ASC")
                driver_iter = cur.fetchall()

                cur.execute("SELECT iteration_coordinate, counter FROM solver_iterations "
                            "ORDER BY counter ASC")
                solver_iter = cur.fetchall()

            con.close()

            split_iter_coord = _coord_split_re.split(iter_coord) if iter_coord is not '' else []

            # grab an array of possible lengths of coordinates
            coord_lengths = [2]  # start with 2 because that is the length of driver iter coords

            for s in solver_iter:
                s_len = len(_coord_split_re.split(s[0]))
                if s_len not in coord_lengths:
                    coord_lengths.append(s_len)

            coord_lengths = sorted(coord_lengths)

            # grab full set of cases to iterate over
            iter_set = self._find_child_cases(iter_coord, split_iter_coord, driver_iter,
                                              solver_iter, recurse, coord_lengths)

            # iterate over set of cases
            for iteration in iter_set:
                if iteration[1] is 'driver':
                    yield self._driver_cases.get_case(iteration[0])
                else:
                    yield self._solver_cases.get_case(iteration[0])

    def _find_child_cases(self, parent_iter_coord, split_parent_iter_coord, driver_iter,
                          solver_iter, recursive, coord_lengths):
        """
        Find all children of a given parent case.

        Parameters
        ----------
        parent_iter_coord : str
            Iteration coordinate of the parent case. If empty string, assumes root is parent.
        split_parent_iter_coord : [str]
            The split parent iteration coordinate.
        driver_iter : [(str)]
            The ordered list of driver iteration coordinates.
        solver_iter : [(str)]
            The ordered list of solver iteration coordinates.
        recursive : bool
            If True, will grab all successors recursively. Otherwise, will only return direct
            children.
        coord_lengths : [int]
            Sorted array of possible coordinate lengths. Used to determine if case is child
            of another case.

        Returns
        -------
        list of tuples
            List of tuples of the form ('iteration_coordinate', 'type of iteration')
        """
        ret = []

        par_len = len(split_parent_iter_coord)
        par_len_idx = coord_lengths.index(par_len if par_len is not 0 else 2)

        expected_child_length = coord_lengths[par_len_idx + 1] \
            if par_len_idx < len(coord_lengths) - 1 else -1

        print(split_parent_iter_coord, 'par_len:', par_len, coord_lengths, 'child_len:', expected_child_length)

        if parent_iter_coord is '':  # CASE: grabbing children of 'root'
            if len(driver_iter) > 0:  # grabbing all driver cases
                for d in driver_iter:
                    ret.append((d[0], 'driver'))
                    if recursive:
                        ret += self._find_child_cases(d[0], _coord_split_re.split(d[0]),
                                                      driver_iter, solver_iter, recursive,
                                                      coord_lengths)
            elif len(solver_iter) > 0:  # grabbing first layer of solver iterations
                # find the iteration coordinate length of the first layer of solver iterations
                min_iter_len = -1
                if len(coord_lengths) > 1:
                    min_iter_len = coord_lengths[1]

                for s in solver_iter:
                    split_coord = _coord_split_re.split(s[0])
                    if len(split_coord) is min_iter_len:
                        ret.append((s[0], 'solver'))
                        if recursive:
                            ret += self._find_child_cases(s[0], split_coord, driver_iter,
                                                          solver_iter, recursive, coord_lengths)
        else:  # CASE: grabbing children of a case
            for s in solver_iter:
                print(_coord_split_re.split(s[0]), 'child_len:', len(_coord_split_re.split(s[0])))
                print('is child?', self._is_case_child(parent_iter_coord, s[0], expected_child_length))
                if self._is_case_child(parent_iter_coord, s[0], expected_child_length):
                    ret.append((s[0], 'solver'))
                    if recursive:
                        ret += self._find_child_cases(s[0], _coord_split_re.split(s[0]),
                                                      driver_iter, solver_iter, recursive,
                                                      coord_lengths)

        return ret

    def _is_case_child(self, parent_coordinate, coordinate, expected_child_length):
        """
        Tell if the given case is a child case of the parent.

        Parameters
        ----------
        parent_coordinate : str
            The iteration coordinate of the potential parent.
        coordinate : str
            Iteration coordinate of the case we want to test.
        expected_child_length : int
            Expected length of the split child iteration coordinate

        Returns
        -------
        bool
            True if the given coordinate indicates that the case is a child of the
            given parent case. False otherwise.
        """
        split_coord = _coord_split_re.split(coordinate)
        print(coordinate.startswith(parent_coordinate), len(split_coord), expected_child_length)
        if coordinate.startswith(parent_coordinate) and len(split_coord) is expected_child_length:
            return True

        return False

    def _get_case_sysvars(self, case, get_outputs=True):
        """
        Get the set of output or input variables and their values for a given case.

        Parameters
        ----------
        case : Case
            The case whose variables will be returned.
        get_outputs : bool, optional
            indicates if the returned set should contain outputs. If false, returns inputs.

        Returns
        -------
        dictionary
            dictionary of global variable names to their values. None if no system iterations
            were recorded.
        """
        variables = {}
        if get_outputs and case.outputs is None:
            return variables

        if get_outputs:
            for abs_name in case.outputs.absolute_names():
                variables[abs_name] = {'value': case.outputs[abs_name]}
                if case.residuals and abs_name in case.residuals.absolute_names():
                    variables[abs_name]['residuals'] = case.residuals[abs_name]
                else:
                    variables[abs_name]['residuals'] = 'Not Recorded'
        elif case.inputs is not None:
            for abs_name in case.inputs.absolute_names():
                if abs_name not in variables:
                    variables[abs_name] = {'value': case.inputs[abs_name]}

        return variables

    def _get_all_sysvars(self, get_outputs=True):
        """
        Get the set of output or input variables and their values.

        Parameters
        ----------
        get_outputs : bool, optional
            indicates if the returned set should contain outputs. If false, returns inputs.

        Returns
        -------
        dictionary
            dictionary of global variable names to their values. None if no system iterations
            were recorded.
        """
        coords = self._system_cases.keys

        # store the iteration coordinates without iteration numbers.
        # coord_map intializes each iter_key to False, indicating we haven't
        # grabbed values from this system
        coord_map = {}
        for c in coords:
            split_iter = _coord_split_re.split(c)
            iter_key = ':'.join(split_iter)
            coord_map[iter_key] = False

        # didn't record any system iterations, return None
        if len(coord_map) is 0:
            return None

        variables = {}
        iteration_num = -1
        # iterate over cases from end to start, unless we've grabbed values from
        # every system
        while not self._has_all_values(coord_map):
            iteration = self._system_cases.keys[iteration_num]
            iteration_num -= 1
            split_iter = _coord_split_re.split(iteration)
            iter_key = ':'.join(split_iter)

            # if coord_map[iter_key] is False, we haven't grabbed variable values
            # from this system
            if not coord_map[iter_key]:
                coord_map[iter_key] = True
                case = self._system_cases.get_case(iteration)
                if get_outputs and case.outputs is None:
                    continue
                if not get_outputs and case.inputs is None:
                    continue

                if get_outputs:
                    for abs_name in case.outputs.absolute_names():
                        if abs_name not in variables:
                            variables[abs_name] = {'value': case.outputs[abs_name]}
                            if case.residuals and abs_name in case.residuals.absolute_names():
                                variables[abs_name]['residuals'] = case.residuals[abs_name]
                            else:
                                variables[abs_name]['residuals'] = 'Not Recorded'
                elif case.inputs is not None:
                    for abs_name in case.inputs.absolute_names():
                        if abs_name not in variables:
                            variables[abs_name] = {'value': case.inputs[abs_name]}

        return variables

    def _has_all_values(self, coord_map):
        """
        Tell if all variables from every recorded system have been iterated over.

        Parameters
        ----------
        coord_map : dict
            maps stripped iteration coordinates to a bool indicating whether or not the system(s)
            associated with that iteration coordinate have been iterated over.

        Returns
        -------
        bool
            True if coord_map is True for each key, False otherwise.
        """
        for coord in coord_map:
            if not coord_map[coord]:
                return False
        return True

    def _write_outputs(self, in_or_out, comp_type, outputs, hierarchical, print_arrays,
                       out_stream):
        """
        Write table of variable names, values, residuals, and metadata to out_stream.

        The output values could actually represent input variables.
        In this context, outputs refers to the data that is being logged to an output stream.

        Parameters
        ----------
        in_or_out : str, 'input' or 'output'
            indicates whether the values passed in are from inputs or output variables.
        comp_type : str, 'Explicit' or 'Implicit'
            the type of component with the output values.
        outputs : list
            list of (name, dict of vals and metadata) tuples.
        hierarchical : bool
            When True, human readable output shows variables in hierarchical format.
        print_arrays : bool
            When False, in the columnar display, just display norm of any ndarrays with size > 1.
            The norm is surrounded by vertical bars to indicate that it is a norm.
            When True, also display full values of the ndarray below the row. Format  is affected
            by the values set with numpy.set_printoptions
            Default is False.
        out_stream : file-like object
            Where to send human readable output.
            Set to None to suppress.
        """
        if out_stream is None:
            return

        # Make a dict of outputs. Makes it easier to work with in this method
        dict_of_outputs = OrderedDict()
        for name, vals in outputs:
            dict_of_outputs[name] = vals

        allprocs_abs_names = {
            'input': dict_of_outputs.keys(),
            'output': dict_of_outputs.keys()
        }

        write_outputs(in_or_out, comp_type, dict_of_outputs, hierarchical, print_arrays, out_stream,
                      'model', allprocs_abs_names)


class CaseTable(object):
    """
    Base class for wrapping case tables in a recording database.

    Attributes
    ----------
    format_version : int
        The version of the format assumed when loading the file.
    num_cases : int
        The number of cases contained in the recorded file.
    keys : tuple
        Case string identifiers available in this CaseReader.
    _filename : str
        The name of the file from which the recorded cases are to be loaded.
    _abs2prom : {'input': dict, 'output': dict}
        Dictionary mapping absolute names to promoted names.
    _abs2meta : dict
        Dictionary mapping absolute variable names to variable metadata.
    _prom2abs : {'input': dict, 'output': dict}
        Dictionary mapping promoted names to absolute names.
    _cases : dict
        Dictionary mapping iteration coordinates to cases that have already been loaded.
    """

    def __init__(self, filename, version, table, index, abs2prom, abs2meta, prom2abs):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The name of the recording file from which to instantiate the case reader.
        format_version : int
            The version of the format assumed when loading the file.
        table : str
            The name of the table in the database.
        index : str
            The name of the case index column in the table.
        abs2prom : {'input': dict, 'output': dict}
            Dictionary mapping absolute names to promoted names.
        abs2meta : dict
            Dictionary mapping absolute variable names to variable metadata.
        prom2abs : {'input': dict, 'output': dict}
            Dictionary mapping promoted names to absolute names.
        """
        self._filename = filename
        self._format_version = format_version
        self._table_name = table
        self._index_name = index
        self._abs2prom = abs2prom
        self._abs2meta = abs2meta
        self._prom2abs = prom2abs
        self._cases = {}

    def count(self):
        """
        Get the number of cases stored in the table
        """
        with sqlite3.connect(self._filename) as con:
            cur = con.cursor()
            cur.execute("SELECT count(*) FROM %s" % self._table_name)
            rows = cur.fetchall()

        con.close()

        return rows[0][0]

    def list_cases(self, source=None, recurse=False, flat=False):
        """
        Get list of case names for cases in the problem_cases table.

        Parameters
        ----------
        source : str, optional
            If not None, only cases that have the specified source will be returned
        recurse : bool, optional
            If True, will enable iterating over all successors in case hierarchy
            rather than just the direct children. Defaults to False.
        flat : bool, optional
            If False and there are child cases, then a nested ordered dictionary
            is returned rather than an iterator.
        """
        with sqlite3.connect(self._filename) as con:
            cur = con.cursor()
            cur.execute("SELECT %s FROM %s" % (self._index_name, self._table_name))
            rows = cur.fetchall()

        con.close()

        if source:
            return [row[0] for row in rows if self._get_source(row[0]) == source]
        else:
            return [row[0] for row in rows]

    def get_case(self, case_name):
        """
        Get a case from the database.

        Parameters
        ----------
        case_name : str
            The string-identifier of the case to be retrieved.

        Returns
        -------
            An instance of a Driver Case populated with data from the
            specified case/iteration.
        """
        # check to see if we've already cached this case
        if case_name in self._cases:
            return self._cases[case_name]

        # we don't have it, so fetch and extract it
        with sqlite3.connect(self._filename) as con:
            cur = con.cursor()
            cur.execute("SELECT * FROM %s WHERE %s=%s" %
                        (self._table_name, self._index_name, self._index_name))
            row = cur.fetchone()

        con.close()

        case = self._extract_case_from_row(row)

        # save so we don't query again
        self._cases[case_name] = case

        return case

    def get_iteration_coordinate(self, case_id):
        """
        Return the iteration coordinate.

        Parameters
        ----------
        case_id : int
            The case number that we want the iteration coordinate for.

        Returns
        -------
        iteration_coordinate : str
            The iteration coordinate.
        """
        if isinstance(case_id, int):
            # If case_id is an integer, assume the user
            # wants a case as an index
            iteration_coordinate = self.keys[case_id]  # handles negative indices for example
        else:
            # Otherwise assume we were given the case string identifier
            iteration_coordinate = case_id

        return iteration_coordinate

    def cases(self):
        """
        Iterate over all cases, while storing them into memory.
        """
        with sqlite3.connect(self._filename) as con:
            cur = con.cursor()
            cur.execute("SELECT * FROM %s" % self._table_name)
            rows = cur.fetchall()
            for row in rows:
                case = self._extract_case_from_row(row)
                self._cases[case.iteration_coordinate] = case
                yield case

        con.close()

    def _load_cases(self):
        """
        Load all cases into memory.
        """
        for case in self.cases():
            # self._cases[case.iteration_coordinate] = case
            pass

    def _get_source(self, iteration_coordinate):
        """
        Get pathname of system that is the source of the iteration.

        Parameters
        ----------
        iteration_coordinate : str
            The full unique identifier for this iteration.

        Returns
        -------
        str
            The pathname of the system that is the source of the iteration.
        """
        path = []
        parts = _coord_split_re.split(iteration_coordinate)
        for part in parts:
            if (_coord_system_re.search(part) is not None):
                if ':' in part:
                    # get rid of 'rank#:'
                    part = part.split(':')[1]
                path.append(part.split('.')[0])

        # return pathname of the system
        return '.'.join(path)

    def _get_first(self, source):
        """
        Get the first case from the specified source.

        Parameters
        ----------
        source : str
            The source.

        Returns
        -------
        Case
            The first case from the specified source.
        """
        for case in self.cases():
            print('checking source:', case.iteration_coordinate, case.source, 'vs', source)
            if case.source == source:
                break

        return case


class DriverCases(CaseTable):
    """
    Case specific to the entries that might be recorded in a Driver iteration.

    Attributes
    ----------
    _var_settings : dict
        Dictionary mapping absolute variable names to variable settings.
    """

    def __init__(self, filename, format_version, abs2prom, abs2meta, prom2abs, var_settings):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The name of the recording file from which to instantiate the case reader.
        format_version : int
            The version of the format assumed when loading the file.
        abs2prom : {'input': dict, 'output': dict}
            Dictionary mapping absolute names to promoted names.
        abs2meta : dict
            Dictionary mapping absolute variable names to variable metadata.
        prom2abs : {'input': dict, 'output': dict}
            Dictionary mapping promoted names to absolute names.
        var_settings : dict
            Dictionary mapping absolute variable names to variable settings.
        """
        super(DriverCases, self).__init__(filename, format_version,
                                          'driver_iterations', 'iteration_coordinate',
                                          abs2prom, abs2meta, prom2abs)
        self._var_settings = var_settings

    def _extract_case_from_row(self, row, deriv_row=None):
        """
        Pull data out of a queried SQLite row.

        Parameters
        ----------
        row : (id, counter, iter_coordinate, timestamp, success, msg, inputs, outputs)
            Queried SQLite driver table row.

        Returns
        -------
        Case
            Case for associated row.
        """
        if self._format_version >= 3:
            inputs_array = json_to_np_array(row['inputs'], self._abs2meta)
            outputs_array = json_to_np_array(row['outputs'], self._abs2meta)
        elif self._format_version in (1, 2):
            inputs_array = blob_to_array(row['inputs'])
            outputs_array = blob_to_array(row['outputs'])

        if deriv_row:
            totals_array = blob_to_array(deriv_row['derivatives'])
        else:
            totals_array = None

        return Case('driver', iteration_coordinate=row['iteration_coordinate'],
                    timestamp=row['timestamp'], success=row['success'], msg=row['msg'],
                    outputs=outputs_array, inputs=inputs_array, jacobian=totals_array,
                    prom2abs=self._prom2abs, abs2prom=self._abs2prom, abs2meta=self._abs2meta,
                    voi_meta=self._var_settings)

    def cases(self):
        """
        Iterate over all driver cases, while storing them into memory.
        """
        with sqlite3.connect(self._filename) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute("SELECT * FROM %s" % self._table_name)
            rows = cur.fetchall()

            for row in rows:
                if self._format_version > 1:
                    # fetch associated derivative data, if available
                    cur.execute("SELECT * FROM driver_derivatives WHERE iteration_coordinate='%s'"
                                % row['iteration_coordinate'])
                    deriv_row = cur.fetchone()
                else:
                    deriv_row = None

                case = self._extract_case_from_row(row, deriv_row)
                self._cases[case.iteration_coordinate] = case
                yield case

        con.close()

    def get_case(self, case_id):
        """
        Get a case from the database.

        Parameters
        ----------
        case_id : int or str
            The integer index or string-identifier of the case to be retrieved.
        scaled : bool
            If True, return variables scaled. Otherwise, return physical values.

        Returns
        -------
            An instance of a Driver Case populated with data from the
            specified case/iteration.
        """
        # check to see if we've already cached this case
        iteration_coordinate = self.get_iteration_coordinate(case_id)
        if iteration_coordinate in self._cases:
            case = self._cases[iteration_coordinate]
        else:
            # Get an unscaled case if does not already exist in _cases
            with sqlite3.connect(self._filename) as con:
                con.row_factory = sqlite3.Row
                cur = con.cursor()

                # fetch driver iteration data
                cur.execute("SELECT * FROM driver_iterations WHERE "
                            "iteration_coordinate=:iteration_coordinate",
                            {"iteration_coordinate": iteration_coordinate})
                driver_row = cur.fetchone()

                # fetch associated derivative data, if available
                if self._format_version > 1:
                    cur.execute("SELECT * FROM driver_derivatives WHERE "
                                "iteration_coordinate=:iteration_coordinate",
                                {"iteration_coordinate": iteration_coordinate})
                    deriv_row = cur.fetchone()
                else:
                    deriv_row = None

            con.close()

            # Initialize the Case object from the fetched data
            case = self._extract_case_from_row(driver_row, deriv_row)

            # save so we don't query again
            self._cases[case.iteration_coordinate] = case

        return case

    def list_sources(self):
        """
        Get list of problems that recorded data in this table.
        """
        return ['driver']

    def _get_source(self, iteration_coordinate):
        """
        Get the source of the iteration.

        Parameters
        ----------
        iteration_coordinate : str
            The full unique identifier for this iteration.

        Returns
        -------
        str
            'driver' (all cases in this table come from the driver).
        """
        return 'driver'


class ProblemCases(CaseTable):
    """
    Case specific to the entries that might be recorded in a Driver iteration.
    """

    def __init__(self, filename, format_version, abs2prom, abs2meta, prom2abs):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The name of the recording file from which to instantiate the case reader.
        format_version : int
            The version of the format assumed when loading the file.
        abs2prom : {'input': dict, 'output': dict}
            Dictionary mapping absolute names to promoted names.
        abs2meta : dict
            Dictionary mapping absolute variable names to variable metadata.
        prom2abs : {'input': dict, 'output': dict}
            Dictionary mapping promoted names to absolute names.
        """
        super(ProblemCases, self).__init__(filename, format_version,
                                           'problem_cases', 'case_name',
                                           abs2prom, abs2meta, prom2abs)

    def _extract_case_from_row(self, row):
        """
        Pull data out of a queried SQLite row.

        Parameters
        ----------
        row : (id, counter, iter_coordinate, timestamp, success, msg, outputs)
            Queried SQLite problems table row.

        Returns
        -------
        Case
            Case for associated row.
        """
        idx, counter, case_name, timestamp, success, msg, \
            outputs_text, = row

        if self._format_version >= 3:
            outputs_array = json_to_np_array(outputs_text, self._abs2meta)
        elif self._format_version in (1, 2):
            outputs_array = blob_to_array(outputs_text)

        case = Case('problem', case_name, timestamp, success, msg, outputs=outputs_array,
                    prom2abs=self._prom2abs, abs2prom=self._abs2prom, abs2meta=self._abs2meta)
        return case

    def list_sources(self):
        """
        Get list of problems that recorded data in this table.
        """
        return ['problem']

    def _get_source(self, iteration_coordinate):
        """
        Get the source of the iteration.

        Parameters
        ----------
        iteration_coordinate : str
            The full unique identifier for this iteration.

        Returns
        -------
        str
            'problem' (all cases in this table come from the problem).
        """
        return 'problem'


class SystemCases(CaseTable):
    """
    Case specific to the entries that might be recorded in a System iteration.
    """

    def __init__(self, filename, format_version, abs2prom, abs2meta, prom2abs):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The name of the recording file from which to instantiate the case reader.
        format_version : int
            The version of the format assumed when loading the file.
        abs2prom : {'input': dict, 'output': dict}
            Dictionary mapping absolute names to promoted names.
        abs2meta : dict
            Dictionary mapping absolute variable names to variable metadata.
        prom2abs : {'input': dict, 'output': dict}
            Dictionary mapping promoted names to absolute names.
        """
        super(SystemCases, self).__init__(filename, format_version,
                                          'system_iterations', 'iteration_coordinate',
                                          abs2prom, abs2meta, prom2abs)

    def _extract_case_from_row(self, row):
        """
        Pull data out of a queried SQLite row.

        Parameters
        ----------
        row : (id, counter, iter_coordinate, timestamp, success, msg, inputs, outputs, residuals)
            Queried SQLite systems table row.

        Returns
        -------
        Case
            Case for associated row.
        """
        idx, counter, iter_coord, timestamp, success, msg, inputs_text,\
            outputs_text, residuals_text = row

        if self._format_version >= 3:
            inputs_array = json_to_np_array(inputs_text, self._abs2meta)
            outputs_array = json_to_np_array(outputs_text, self._abs2meta)
            residuals_array = json_to_np_array(residuals_text, self._abs2meta)
        elif self._format_version in (1, 2):
            inputs_array = blob_to_array(inputs_text)
            outputs_array = blob_to_array(outputs_text)
            residuals_array = blob_to_array(residuals_text)

        source = self._get_source(iter_coord)

        case = Case(source, iter_coord, timestamp, success, msg,
                    inputs=inputs_array, outputs=outputs_array, residuals=residuals_array,
                    prom2abs=self._prom2abs, abs2prom=self._abs2prom, abs2meta=self._abs2meta)

        return case

    def list_sources(self):
        """
        Get list of systems that recorded data in this table.
        """
        return set([self._get_source(case) for case in self.list_cases()])


class SolverCases(CaseTable):
    """
    Case specific to the entries that might be recorded in a Solver iteration.
    """

    def __init__(self, filename, format_version, abs2prom, abs2meta, prom2abs):
        """
        Initialize.

        Parameters
        ----------
        filename : str
            The name of the recording file from which to instantiate the case reader.
        format_version : int
            The version of the format assumed when loading the file.
        abs2prom : {'input': dict, 'output': dict}
            Dictionary mapping absolute names to promoted names.
        abs2meta : dict
            Dictionary mapping absolute variable names to variable metadata.
        prom2abs : {'input': dict, 'output': dict}
            Dictionary mapping promoted names to absolute names.
        """
        super(SolverCases, self).__init__(filename, format_version,
                                          'solver_iterations', 'iteration_coordinate',
                                          abs2prom, abs2meta, prom2abs)

    def _extract_case_from_row(self, row):
        """
        Pull data out of a queried SQLite row.

        Parameters
        ----------
        row : (id, counter, iter_coordinate, timestamp, success, msg, abs_err, rel_err,
               inputs, outputs, residuals)
            Queried SQLite solvers table row.

        Returns
        -------
        Case
            Case for associated row.
        """
        idx, counter, iter_coord, timestamp, success, msg, abs_err, rel_err, \
            input_text, output_text, residuals_text = row

        if self._format_version >= 3:
            input_array = json_to_np_array(input_text, self._abs2meta)
            output_array = json_to_np_array(output_text, self._abs2meta)
            residuals_array = json_to_np_array(residuals_text, self._abs2meta)
        elif self._format_version in (1, 2):
            input_array = blob_to_array(input_text)
            output_array = blob_to_array(output_text)
            residuals_array = blob_to_array(residuals_text)

        source = self._get_source(iter_coord)

        case = Case(source, iter_coord, timestamp, success, msg,
                    inputs=input_array, outputs=output_array, residuals=residuals_array,
                    abs_err=abs_err, rel_err=rel_err,
                    prom2abs=self._prom2abs, abs2prom=self._abs2prom, abs2meta=self._abs2meta)

        return case

    def list_sources(self):
        """
        Get list of solvers that recorded data in this table.
        """
        return set([self._get_source(case) for case in self.list_cases()])

    def _get_source(self, iteration_coordinate):
        """
        Get pathname of solver that is the source of the iteration.

        Parameters
        ----------
        iteration_coordinate : str
            The full unique identifier for this iteration.

        Returns
        -------
        str
            The pathname of the solver that is the source of the iteration.
        """
        return super(SolverCases, self)._get_source(iteration_coordinate) + '.nonlinear_solver'
