import numpy as np
import renderapi
import argschema
from .schemas import EMA_Schema
from . import utils
import time
import scipy.sparse as sparse
from scipy.sparse import csr_matrix
import warnings
import os
import sys
import logging
import json
import h5py
warnings.simplefilter(action='ignore', category=FutureWarning)
warnings.resetwarnings()

logger = logging.getLogger(__name__)


def calculate_processing_chunk(fargs):
    t0 = time.time()
    # set up for calling using multiprocessing pool
    [pair, args, tspecs, col_ind, ncol] = fargs

    tile_ids = np.array([t.tileId for t in tspecs])

    dbconnection = utils.make_dbconnection(args['pointmatch'])
    sorter = np.argsort(tile_ids)

    # get point matches
    nmatches = utils.get_matches(
            pair['section1'],
            pair['section2'],
            args['pointmatch'],
            dbconnection)

    # extract IDs for fast checking
    pid_set = set(m['pId'] for m in nmatches)
    qid_set = set(m['qId'] for m in nmatches)

    tile_set = set(tile_ids)

    pid_set.intersection_update(tile_set)
    qid_set.intersection_update(tile_set)

    matches = [m for m in nmatches if m['pId']
               in pid_set and m['qId'] in qid_set]
    del nmatches

    if len(matches) == 0:
        logger.debug(
            "no tile pairs in "
            "stack for pointmatch groupIds %s and %s" % (
                pair['section1'], pair['section2']))
        return None

    pids = np.array([m['pId'] for m in matches])
    qids = np.array([m['qId'] for m in matches])

    logger.debug(
            "loaded %d matches, using %d, "
            "for groupIds %s and %s in %0.1f sec "
            "using interface: %s" % (
                len(pid_set.union(qid_set)),
                len(matches),
                pair['section1'],
                pair['section2'],
                time.time() - t0,
                args['pointmatch']['db_interface']))

    # for the given point matches, these are the indices in tile_ids
    # these determine the column locations in A for each tile pair
    # this is a fast version of np.argwhere() loop
    pinds = sorter[np.searchsorted(tile_ids, pids, sorter=sorter)]
    qinds = sorter[np.searchsorted(tile_ids, qids, sorter=sorter)]

    tilepair_weightfac = tilepair_weight(
        pair['z1'],
        pair['z2'],
        args['matrix_assembly'])

    wts = []
    pblocks = []
    qblocks = []
    rhss = []
    used = []
    for k, match in enumerate(matches):

        pblock, qblock, weights, rhs = utils.blocks_from_tilespec_pair(
                tspecs[pinds[k]],
                tspecs[qinds[k]],
                match,
                col_ind[pinds[k]],
                col_ind[qinds[k]],
                ncol,
                args['matrix_assembly'])

        if pblock is None:
            continue

        pblocks.append(pblock)
        qblocks.append(qblock)
        wts.append(weights * tilepair_weightfac)
        rhss.append(rhs)

        # note both as used
        used.append(tspecs[pinds[k]].tileId)
        used.append(tspecs[qinds[k]].tileId)

    chunk = {}
    chunk['zlist'] = np.array([pair['z1'], pair['z2']])
    chunk['tiles_used'] = used
    chunk['block'] = sparse.vstack(pblocks) - sparse.vstack(qblocks)
    chunk['weights'] = np.concatenate(wts)
    chunk['rhs'] = np.concatenate(rhss)

    return chunk


def tilepair_weight(z1, z2, matrix_assembly):
    if matrix_assembly['explicit_weight_by_depth'] is not None:
        ind = matrix_assembly['depth'].index(int(np.abs(z1 - z2)))
        tp_weight = matrix_assembly['explicit_weight_by_depth'][ind]
    else:
        if z1 == z2:
            tp_weight = matrix_assembly['montage_pt_weight']
        else:
            tp_weight = matrix_assembly['cross_pt_weight']
            if matrix_assembly['inverse_dz']:
                tp_weight = tp_weight/(np.abs(z2 - z1) + 1)
    return tp_weight


class EMaligner(argschema.ArgSchemaParser):
    default_schema = EMA_Schema
    renderapi.client.WithPool = \
        renderapi.external.processpools.stdlib_pool.WithThreadPool

    def run(self):
        logger.setLevel(self.args['log_level'])
        utils.logger.setLevel(self.args['log_level'])
        t0 = time.time()
        zvals = np.arange(
            self.args['first_section'],
            self.args['last_section'] + 1)

        # the parallel workers will need this stack ready
        if self.args['output_mode'] == 'stack':
            utils.create_or_set_loading(self.args['output_stack'])

        # montage
        if self.args['solve_type'] == 'montage':
            zvals = utils.get_z_values_for_stack(
                    self.args['input_stack'],
                    zvals)
            for z in zvals:
                self.results = self.assemble_and_solve(np.array([z]))

        # 3D
        elif self.args['solve_type'] == '3D':
            self.results = self.assemble_and_solve(zvals)

        if (self.args['output_mode'] == 'stack') & self.args['close_stack']:
            utils.set_complete(self.args['output_stack'])

        logger.info(' total time: %0.1f' % (time.time() - t0))

    def assemble_and_solve(self, zvals):
        t0 = time.time()

        # read in the tilespecs
        self.resolvedtiles = utils.get_resolved_tilespecs(
            self.args['input_stack'],
            self.args['transformation'],
            self.args['n_parallel_jobs'],
            zvals,
            fullsize=self.args['fullsize_transform'],
            order=self.args['poly_order'])

        if self.args['ingest_from_file'] != '':
            assemble_result = self.assemble_from_hdf5(
                self.args['ingest_from_file'],
                zvals,
                read_data=False)
            results = {}
            results['x'] = assemble_result['x']

        else:
            if self.args['assemble_from_file'] != '':
                assemble_result = self.assemble_from_hdf5(
                    self.args['assemble_from_file'],
                    zvals)
            else:
                assemble_result = self.assemble_from_db(zvals)

            self.ntiles_used = np.count_nonzero(assemble_result['tiles_used'])
            logger.info(' A created in %0.1f seconds' % (time.time() - t0))

            if self.args['profile_data_load']:
                raise utils.EMalignerException(
                    "exiting after timing profile")

            # solve
            message, results = \
                self.solve_or_not(
                    assemble_result['A'],
                    assemble_result['weights'],
                    assemble_result['reg'],
                    assemble_result['x'],
                    assemble_result['rhs'])
            logger.info('\n' + message)
            del assemble_result['A']

        if results:
            utils.update_tilespecs(
                    self.resolvedtiles,
                    results['x'],
                    assemble_result['tiles_used'])
            scales = np.array(
                    [np.array(t.tforms[-1].scale)
                     for t in self.resolvedtiles.tilespecs])
            smn = scales.mean(axis=0)
            print(scales.min(axis=0))
            print(scales.max(axis=0))
            ssd = scales.std(axis=0)
            logger.info("\n scales: %0.2f +/- %0.2f, %0.2f +/- %0.2f" % (
                smn[0], ssd[0], smn[1], ssd[1]))
            if self.args['output_mode'] == 'stack':
                res_for_file = {a: b for a, b in results.items() if a != 'x'}
                self.args['output_stack'] = utils.write_to_new_stack(
                        self.resolvedtiles,
                        self.args['output_stack'],
                        self.args['render_output'],
                        self.args['overwrite_zlayer'],
                        # for file output, these go too
                        self.args,
                        res_for_file)
                if self.args['render_output'] == 'stdout':
                    logger.info(message)
            del assemble_result['x']

        return results

    def assemble_from_hdf5(self, filename, zvals, read_data=True):
        assemble_result = {}

        with h5py.File(filename, 'r') as f:
            assemble_result['tids'] = np.array(
                f.get('used_tile_ids')[()]).astype('U')
            assemble_result['unused_tids'] = np.array(
                f.get('unused_tile_ids')[()]).astype('U')
            k = 0
            assemble_result['x'] = []
            while True:
                name = 'transforms_%d' % k
                if name in f.keys():
                    assemble_result['x'].append(f.get(name)[()])
                    k += 1
                else:
                    break

            if len(assemble_result['x']) == 1:
                n = assemble_result['x'][0].size
                assemble_result['x'] = np.array(
                    assemble_result['x']).flatten().reshape((n, 1))
            else:
                assemble_result['x'] = np.transpose(
                    np.array(assemble_result['x']))

            reg = f.get('lambda')[()]
            datafile_names = f.get('datafile_names')[()]
            file_args = json.loads(f.get('input_args')[()][0])

        # get the tile IDs and transforms
        tids = np.array([
            t.tileId for t in self.resolvedtiles.tilespecs])
        assemble_result['tiles_used'] = np.in1d(tids, assemble_result['tids'])

        assemble_result['reg'] = sparse.diags([reg], [0], format='csr')

        if read_data:
            data = np.array([]).astype('float64')
            weights = np.array([]).astype('float64')
            indices = np.array([]).astype('int64')
            indptr = np.array([]).astype('int64')

            fdir = os.path.dirname(filename)
            i = 0
            for fname in datafile_names:
                with h5py.File(os.path.join(fdir, fname), 'r') as f:
                    data = np.append(data, f.get('data')[()])
                    indices = np.append(indices, f.get('indices')[()])
                    if i == 0:
                        indptr = np.append(indptr, f.get('indptr')[()])
                        i += 1
                    else:
                        indptr = np.append(
                            indptr,
                            f.get('indptr')[()][1:] + indptr[-1])
                    weights = np.append(weights, f.get('weights')[()])
                    logger.info('  %s read' % fname)

            assemble_result['A'] = csr_matrix((data, indices, indptr))
            assemble_result['weights'] = sparse.diags(
                    [weights], [0], format='csr')

        # alert about differences between this call and the original
        for k in file_args.keys():
            if k in self.args.keys():
                if file_args[k] != self.args[k]:
                    logger.warning("for key \"%s\" " % k)
                    logger.warning("  from file: " + str(file_args[k]))
                    logger.warning("  this call: " + str(self.args[k]))
            else:
                logger.warning("for key \"%s\" " % k)
                logger.warning("  file     : " + str(file_args[k]))
                logger.warning("  this call: not specified")

        logger.info("csr inputs read from files listed in : "
                    "%s" % self.args['assemble_from_file'])

        return assemble_result

    def assemble_from_db(self, zvals):
        # create A matrix in compressed sparse row (CSR) format
        CSR_A = self.create_CSR_A(self.resolvedtiles)

        assemble_result = {}
        assemble_result['A'] = CSR_A.pop('A')
        assemble_result['weights'] = CSR_A.pop('weights')
        assemble_result['tiles_used'] = CSR_A.pop('tiles_used')
        assemble_result['reg'] = CSR_A.pop('reg')
        assemble_result['x'] = CSR_A.pop('x')
        assemble_result['rhs'] = CSR_A.pop('rhs')

        # output the regularization vectors to hdf5 file
        if self.args['output_mode'] == 'hdf5':
            alltids = np.array([
                t.tileId for t in self.resolvedtiles.tilespecs])

            utils.write_reg_and_tforms(
                dict(self.args),
                CSR_A['metadata'],
                assemble_result['x'],
                assemble_result['reg'],
                alltids[assemble_result['tiles_used']],
                alltids[np.invert(assemble_result['tiles_used'])])

        return assemble_result

    def create_CSR_A(self, resolved):
        func_result = {
            'A': None,
            'x': None,
            'reg': None,
            'weights': None,
            'rhs': None,
            'tiles_used': None,
            'metadata': None}

        # the processing will be distributed according to these pairs
        pairs = utils.determine_zvalue_pairs(
                resolved,
                self.args['matrix_assembly']['depth'])

        # the column indices for each tilespec
        col_ind = np.cumsum(
                np.hstack((
                    [0],
                    [t.tforms[-1].DOF_per_tile for t in resolved.tilespecs])))

        tile_ids = np.array([t.tileId for t in resolved.tilespecs])
        fargs = [[
            pair,
            self.args,
            [resolved.tilespecs[k] for k in pair['ind']],
            col_ind[pair['ind']],
            col_ind.max()] for pair in pairs]

        with renderapi.client.WithPool(self.args['n_parallel_jobs']) as pool:
            results = np.array(pool.map(calculate_processing_chunk, fargs))

        t_used = np.unique(
                np.concatenate([r['tiles_used'] for r in results if r]))

        func_result['tiles_used'] = np.in1d(tile_ids, t_used)

        func_result['x'] = []
        reg = []
        for t in np.array(resolved.tilespecs)[func_result['tiles_used']]:
            func_result['x'].append(t.tforms[-1].to_solve_vec())
            reg.append(
                    t.tforms[-1].regularization(self.args['regularization']))
        func_result['x'] = np.concatenate(func_result['x'])
        func_result['reg'] = sparse.diags(
                [np.concatenate(reg)], [0], format='csr')

        if self.args['output_mode'] == 'hdf5':
            results = np.array(results)

            if self.args['hdf5_options']['chunks_per_file'] == -1:
                proc_chunks = [np.arange(results.size)]
            else:
                proc_chunks = np.array_split(
                    np.arange(results.size),
                    np.ceil(
                        results.size /
                        self.args['hdf5_options']['chunks_per_file']))

            func_result['metadata'] = []
            for pchunk in proc_chunks:
                Awz = self.concatenate_results(results[pchunk])
                if Awz:
                    fname = self.args['hdf5_options']['output_dir'] + \
                        '/%d_%d.h5' % (Awz[2].min(), Awz[2].max())
                    func_result['metadata'].append(
                        utils.write_chunk_to_file(fname, Awz[0], Awz[1].data))

        else:
            func_result['A'], func_result['weights'], func_result['rhs'], _ = \
                    self.concatenate_results(results)
            slice_ind = np.concatenate(
                    [np.repeat(
                        func_result['tiles_used'][i],
                        resolved.tilespecs[i].tforms[-1].DOF_per_tile)
                     for i in range(tile_ids.size)])
            func_result['A'] = func_result['A'][:, slice_ind]

        return func_result

    def concatenate_results(self, results):
        ind = np.flatnonzero(results)
        if ind.size == 0:
            return None

        A = sparse.vstack([r['block'] for r in results[ind]])
        weights = sparse.diags(
                    [np.concatenate([r['weights'] for r in results[ind]])],
                    [0],
                    format='csr')
        rhs = np.concatenate([r.pop('rhs') for r in results[ind]])
        zlist = np.concatenate([r.pop('zlist') for r in results[ind]])

        return A, weights, rhs, zlist

    def solve_or_not(self, A, weights, reg, x0, rhs):
        # not
        if self.args['output_mode'] in ['hdf5']:
            message = '*****\nno solve for file output\n'
            message += 'solve from the files you just wrote:\n\n'
            message += 'python '
            for arg in sys.argv:
                message += arg+' '
            message = message + '--assemble_from_file ' + \
                self.args['hdf5_options']['output_dir']
            message = message + ' --output_mode none'
            message += '\n\nor, run it again to solve with no output:\n\n'
            message += 'python '
            for arg in sys.argv:
                message += arg + ' '
            message = message.replace(' hdf5 ', ' none ')
            results = None
        else:
            results = utils.solve(A, weights, reg, x0, rhs)
            message = utils.message_from_solve_results(results)

        return message, results


if __name__ == '__main__':  # pragma: no cover
    mod = EMaligner(schema_type=EMA_Schema)
    mod.run()
