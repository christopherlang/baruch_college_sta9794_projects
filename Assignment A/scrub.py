import sys
import os
import json
import datetime as dt
from mpi4py import MPI
import re
import ntpath

# print wdir
# print "/".join([os.getcwd(), "..", "lib"])

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()


def worker(file, row_index, rank, delimiter=",", logger=None,
           enable_debug=False):
    start_row = row_index[0]
    nrows_read = row_index[1] - row_index[0] + 1
    reader = chk.row_reader(file, start_row, nrows_read)

    if logger is not None and enable_debug:
            msg = "rank{0} start index: {1}, nrows: {2}"
            msg = msg.format(rank, start_row, nrows_read)
            logger.debug(msg)

    nrows_parsed = 0
    rows = list()
    for a_row in reader:
        nrows_parsed += 1
        rows.append(a_row)

    if "\r\n" in rows[0] and "\r\n" in rows[-1]:
        rows = [i.split("\r\n")[0] for i in rows]

        if logger is not None and enable_debug:
                msg = "\\r\\n end of line detected"
                logger.debug(msg)

    elif "\n" in rows[0] and "\n" in rows[-1]:
        rows = [i.split("\n")[0] for i in rows]

        if logger is not None and enable_debug:
                msg = "\\n end of line detected"
                logger.debug(msg)

    rows = [i.split(delimiter) for i in rows]

    rows = row_clean(rows, start_row, start_row + nrows_read)

    if logger is not None and enable_debug:
            msg = "rank{0} finished cleaning, nrows {1}"
            msg = msg.format(rank, len(rows))
            logger.debug(msg)

    rows = detect_noise(rows)

    if logger is not None and enable_debug:
            msg = "rank{0} detect noise finished, nrows {1}"
            msg = msg.format(rank, len(rows))
            logger.debug(msg)

    result = dict()
    result['rows_parsed'] = nrows_parsed

    if len(rows['noise_rows']) > 0:
        filename = "noise-rank" + str(rank) + "-"
        filename += dt.datetime.strftime(dt.datetime.utcnow(),
                                         format="%Y%m%dT%H%M%S")
        # filename += dt.datetime.utcnow().isoformat().replace(":", "")
        # filename = filename.replace(".", "_")
        filename = "cache/" + filename + ".txt"

        with open(filename, "w") as f:
            f.writelines([str(i) + "\n" for i in rows['noise_rows']])

    rows.pop('noise_rows')
    result.update(rows)

    return result


def row_clean(rows, start_index, end_index):
    for a_row, row_index, in zip(rows, range(start_index, end_index)):
        a_row[1:3] = [float(a_row[1]), int(a_row[2])]
        a_row += [row_index]

    rows.sort(key=lambda x: x[0])

    result = [tuple(i) for i in rows]

    return result


def detect_noise(rows):
    noise_indices = set()
    result = dict()

    result['noise_rows'] = None
    result['n_duplicates'] = 0
    result['n_wrongLength'] = 0
    result['n_negativeNum'] = 0
    result['n_timestampFormat'] = 0

    for i in range(len(rows)):

        if i >= 1:
            # Starting from row 2, check for duplicates. Probably very slow
            # TODO - Find a way to avoid constantly check if we're starting
            #        from row 1 or above
            if rows[i][:-1] == rows[i - 1][:-1]:
                # This check if current row is the same as before. This only
                # works if row objects are immutable. Therefore, before running
                # detect_noise, row_clean must be run as it converts to tuples
                #
                # This also assumings that rows are sorted by time in ascending
                # order
                noise_indices.add(rows[i][-1])
                result['n_duplicates'] += 1

        # Presuming all rows should have 3 elements, one for each column
        if len(rows[i]) != 4:
            noise_indices.add(rows[i][-1])
            result['n_wrongLength'] += 1

        # Look for negative "price" (index 1) and "units traded" (index 2)
        elif rows[i][1] < 0 or rows[i][2] < 0:
            noise_indices.add(rows[i][-1])
            result['n_negativeNum'] += 1

        if re.search("\d{8}([:]\d{2}){3}([.]\d+)?", rows[i][0]) is None:
            noise_indices.add(rows[i][-1])
            result['n_timestampFormat'] += 1

    # If no noise rows are found based on the above logic, then return None
    if len(noise_indices) != 0:
        result['noise_rows'] = noise_indices

    else:
        result['noise_rows'] = set()

    return result


if __name__ == "__main__":
    # Set working directory to script's directory
    wdir = os.path.dirname(os.path.realpath(__file__))
    os.chdir(wdir)

    sys.path.append("/".join([os.getcwd(), "..", "lib"]))

    import chunkers as chk
    import ResultLogger as rl
    import utils
    import logging as lg

    # Load program settings
    with open("config/assignmentA_config.json", "r") as f:
        configs = json.load(f)

    nchunk = configs['chunk_size']
    n_cores = configs['num_process']
    row_delim = configs['col_delimiter']
    enable_debug = configs['enable_debug']
    noiseloc = configs['noisefileloc']

    # Create execution logger
    # ---------------------------------------------------------------------
    lg.basicConfig(filename='log/example.log', level=lg.DEBUG,
                   format='%(asctime)s : %(levelname)s : %(message)s',
                   datefmt='%Y-%m-%dT%H:%M:%S')

    if enable_debug:
        lg.info("Debug logging is enabled")

    else:
        lg.info("Debug logging is disabled")

    # Get the location of the data file to be parsed
    # -------------------------------------------------------------------------
    dataloc = None
    try:
        dataloc = sys.argv[1]

    except IndexError:
        # No data loc was provided via command line, check config file
        pass

    if dataloc is None:
        try:
            dataloc = configs['dataloc']

        except KeyError:
            # no dataloc was provided in configuration file
            pass

    if dataloc is None:
        raise Exception("No data location was provided")

    if rank == 0:
        # Master process section ----------------------------------------------
        # ---------------------------------------------------------------------

        # Create time tracker -------------------------------------------------
        # ---------------------------------------------------------------------
        import TimeTrack
        tt = TimeTrack.TimeTrack()

        # Create result logger
        # ---------------------------------------------------------------------
        result_log = rl.ResultLogger("result/testresult.txt",
                                     configs['prog_title'])

        nrows = chk.get_nrows(dataloc)

        print("MPI size: " + str(size))
        print("Chunk size: " + str(nchunk))

        result_log.init_section("Program Information", level=0)
        result_log.add_section_kv("MPI size", size)
        result_log.add_section_kv("Chunk size", nchunk)
        result_log.add_section_kv("File name", ntpath.basename(dataloc))
        result_log.add_section_kv("Row count", nrows)
        result_log.exec_section()

        if size == 1:
            lg.info("MPI size {0}, working in serial".format(size))

        else:
            lg.info("MPI size {0}, working in parallel".format(size))

        s_index = 0
        nrows_left = nrows
        nworkers = size

        if nworkers < 1:
            nworkers = 1

        scrub_results = list()

        worker_row_indices = list()
        for i in range(nworkers):
            worker_row_indices.insert(i, [])

        while nrows_left != 0:
            row_indices = utils.size_sequencer(nchunk, nworkers, s_index)

            for a_row_indices in range(len(row_indices)):
                index_interval = row_indices[a_row_indices]
                worker_row_indices[a_row_indices].append(index_interval)

            nrows_left -= nchunk
            s_index += nchunk

            if nchunk > nrows_left:
                nchunk = nrows_left

    else:
        worker_row_indices = None

    if worker_row_indices is not None:
        print(len(worker_row_indices))

    row_indices = comm.scatter(worker_row_indices, root=0)

    if rank == 0:
        extt = TimeTrack.TimeTrack()

    work_result = list()
    for index_interval in row_indices:
        r = worker(dataloc, index_interval, rank, row_delim, lg, enable_debug)
        work_result.append(r)

    scrub_results = comm.gather(work_result, root=0)

    if rank == 0:
        extt.pause_time()

        scrub_results = [item for sublist in scrub_results for item in sublist]
        # Total aggregate count
        r = dict()
        r['rows_parsed'] = 0
        r['n_duplicates'] = 0
        r['n_negativeNum'] = 0
        r['n_wrongLength'] = 0
        r['n_timestampFormat'] = 0

        for a_result in scrub_results:
            r['rows_parsed'] += a_result['rows_parsed']
            r['n_duplicates'] += a_result['n_duplicates']
            r['n_negativeNum'] += a_result['n_negativeNum']
            r['n_wrongLength'] += a_result['n_wrongLength']
            r['n_timestampFormat'] += a_result['n_timestampFormat']

        # Combine noise files
        noise = list()
        if os.path.exists(noiseloc) is not True:
            file = open(noiseloc, "w")
            file.close()

        for a_file in os.listdir("cache"):
            with open("cache/" + a_file, "r") as cachenoisefile:
                with open(noiseloc, "a") as noisefile:
                    noisefile.writelines(cachenoisefile.readlines())

            os.remove("cache/" + a_file)

        tt.pause_time()

        result_log.init_section("Analysis Output", level=0)
        result_log.add_section_kv("Execution start time",
                                  tt.start_time_pretty())
        result_log.add_section_kv("Execution end time", tt.end_time_pretty())
        result_log.add_section_kv("Execution elapsed time",
                                  tt.elapsed_pretty())
        result_log.add_section_kv("Row parse elapsed time",
                                  extt.elapsed_pretty())
        result_log.add_section_kv("Row count", r['rows_parsed'])

        velocity = r['rows_parsed'] / extt.elapsed_seconds()
        velocity = round(velocity, 2)
        result_log.add_section_kv("Velocity (rows parsed / sec)", velocity)
        result_log.add_section_kv("Total # noise rows",
                                  (r['n_duplicates'] + r['n_negativeNum'] +
                                   r['n_wrongLength'] +
                                   r['n_timestampFormat']))
        result_log.add_section_kv("Noise rows (duplicates)",
                                  r['n_duplicates'])
        result_log.add_section_kv("Noise rows (negative num.)",
                                  r['n_negativeNum'])
        result_log.add_section_kv("Noise rows (# of columns)",
                                  r['n_wrongLength'])
        result_log.add_section_kv("Noise rows (timestamp format)",
                                  r['n_timestampFormat'])

        result_log.exec_section()
