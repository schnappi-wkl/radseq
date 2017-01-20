#!/usr/bin/env python
"""
Multi-processing STRUCTURE (Pritchard et al 2000) wrapper for RAD-seq data.
Takes a `.vcf` as input file and then creates a number of replicate datasets,
each with a different pseudo-random subsampling of one SNP per RAD contig.
Then, it runs the replicate datasets through STRUCTURE across multiple threads,
and summarises the outcome with CLUMPP (Jakobsson and Rosenberg 2007).
Finally, it assesses the number of potential clusters using the Puechmaille
2016 method (only suitable for certain datasets).

Note: STILL NEEDS TO BE MODIFIED FOR GENERAL USE. Input file (`.vcf`) should
be sorted by CHROM, and different params files need to be present in current
path.
"""
from multiprocessing import Pool, Lock
from functools import partial
import subprocess
import argparse
import vcf
import random
import sys
import os
import time
import math
import numpy

__author__ = "Pim Bongaerts"
__copyright__ = "Copyright (C) 2016 Pim Bongaerts"
__license__ = "GPL"

GENOTYPE_CONVERSION = {'A': '10', 'T': '11', 'G': '12', 'C': '13', '.': '-9'}
STRUCTURE_ANCESTRY_HEADER = 'Inferred ancestry of individuals:'


def print_msg(msg):
    """ Print message to std out and flush """
    print(msg, end='', flush=True)
    # log_file.write('%s\n' % msg)


def dict_from_popfile(pop_filename):
    """ Initialise dict with indvs and pop assignments from popfile """
    indvs_pops = {}
    pop_file = open(pop_filename, 'r')
    for line in pop_file:
        cols = line.rstrip().split('\t')
        indvs_pops[cols[0]] = cols[1]
    return indvs_pops


def select_random_snps(vcf_filename, replicates_IDs):
    """ Return a dict with a random SNP per CHROM for each replicate """
    # Initialise dict with a list for each replicate
    selected_snps = {}
    for replicate in replicates_IDs:
        selected_snps[replicate] = []

    # Iterate through all SNPs in VCF
    temp_SNPs = []
    previous_CHROM = ''
    vcf_reader = vcf.Reader(open(vcf_filename, 'r'))
    for record in vcf_reader:
        if previous_CHROM not in ('', record.CHROM):
            # When reaching new CHROM: select one random SNP from list
            # for each replicate
            for replicate in replicates_IDs:
                random_snp = temp_SNPs[random.randint(0, len(temp_SNPs) - 1)]
                selected_snps[replicate].append(random_snp)
            temp_SNPs = []
        # Generate list of all SNPs in CHROM
        temp_SNPs.append('{0}_{1}'.format(record.CHROM, record.POS))
        previous_CHROM = record.CHROM
    return selected_snps


def get_structure_genotype(genotype):
    """ Convert VCF to STRUCTURE genotype """
    if not genotype:
        genotype = './.'
    genotype1 = GENOTYPE_CONVERSION[genotype[0]]
    genotype2 = GENOTYPE_CONVERSION[genotype[2]]
    return genotype1, genotype2


def init_output_folder(vcf_filename):
    """ Initialise output folder and logfile """
    timestamp = int(time.time())
    output_folder = vcf_filename.replace('.vcf', '_%s' % timestamp)
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    # log_file = open('%s/logfile.txt' % output_folder, 'w')
    return output_folder


def init_genotype_dict(indvs_pops):
    """ Initialise dict to store genotypes """
    structure_genotypes = {}
    for indiv in indvs_pops:
        structure_genotypes[indiv] = {}
        structure_genotypes[indiv][0] = []
        structure_genotypes[indiv][1] = []
    return structure_genotypes


def run_structure(num_indvs, num_loci, num_K, replicate):
    """ Run STRUCTURE (external) for given replicate """
    output_filename = replicate.replace('.str', '_{0}.out'.format(num_K))
    stdout_filename = replicate.replace('.str', '_{0}.log'.format(num_K))
    stdout_file = open(stdout_filename, 'w')
    args = ['structure',
            '-i', replicate,
            '-o', output_filename,
            # '-L', str(num_indvs),
            # '-N', str(num_loci),
            '-K', str(num_K)]
    subprocess.call(args, stdout=stdout_file)


def create_clumpp_paramfile(clumpp_prefix, K, num_indvs, num_reps):
    """ Generate paramfile for CLUMPP """
    clumpp_filename = '{0}.ind'.format(clumpp_prefix)
    out_filename = '{0}.out'.format(clumpp_prefix)
    misc_filename = '{0}.misc'.format(clumpp_prefix)
    permute_filename = '{0}_perm'.format(clumpp_prefix)
    paramfilename = '{0}.paramfile'.format(clumpp_prefix)
    # Contents to write to paramfile
    contents = ['DATATYPE 0',
                'INDFILE {}'.format(clumpp_filename),
                'OUTFILE {}'.format(out_filename),
                'MISCFILE {}'.format(misc_filename),
                'K {}'.format(K),
                'C {}'.format(num_indvs),
                'R {}'.format(num_reps),
                'M 2', 'S 2', 'W 1', 'PRINT_PERMUTED_DATA 2',
                'GREEDY_OPTION 2', 'REPEATS 1000',
                'PRINT_RANDOM_INPUTORDER 0',
                'PERMUTED_DATAFILE {}'.format(permute_filename),
                'PRINT_EVERY_PERM 0', 'OVERRIDE_WARNINGS 0', 'ORDER_BY_RUN 1']
    # Create paramfile and output contents to it
    paramfile = open(paramfilename, 'w')
    for line in contents:
        paramfile.write('{}\n'.format(line))
    paramfile.close()
    return paramfilename


def convert_clumpp_to_csv(clumpp_prefix, indvs_pops, num_reps):
    """ Convert all CLUMPP output files to CSV"""
    clumpp_filenames = []
    # Add overall output file to list
    clumpp_filenames.append('{0}.out'.format(clumpp_prefix))
    # Add permuted output files to list
    for x in range(1, num_reps + 1):
        clumpp_filenames.append('{0}_perm_{1}'.format(clumpp_prefix, x))
    # Generate look-up list for individual names
    indiv_name = []
    for indiv in sorted(indvs_pops.keys()):
        indiv_name.append(indiv)
    # Convert output files to CSV
    for clumpp_filename in clumpp_filenames:
        clumpp_file = open(clumpp_filename, 'r')
        csv_output_file = open('{}.csv'.format(clumpp_filename), 'w')
        for line in clumpp_file:
            cols = line.split()
            sample = indiv_name[int(cols[1]) - 1]
            pop_name = indvs_pops[sample]
            clusters = ','.join(cols[5:])
            csv_output_file.write('{0},{1},{2}\n'.format(sample, pop_name,
                                                         clusters))
        clumpp_file.close()
        csv_output_file.close()


def run_clumpp(clumpp_paramfile):
    """ Run CLUMPP (external) for given K """
    stdout_filename = '{0}.log'.format(clumpp_paramfile.replace('.paramfile',
                                                                '.log'))
    stdout_file = open(stdout_filename, 'w')
    args = ['CLUMPP', clumpp_paramfile]
    subprocess.call(args, stdout=stdout_file)


def generate_clumpp_input(clumpp_prefix, replicate_IDs, K):
    """ Harvest STRUCTURE outputs and convert to CLUMPP indfile """
    clumpp_filename = '{0}.ind'.format(clumpp_prefix)
    clumpp_file = open(clumpp_filename, 'w')
    for replicate in replicate_IDs:
        structure_postfix = '_{0}.out_f'.format(K)
        structure_filename = replicate.replace('.str', structure_postfix)
        structure_file = open(structure_filename, 'r')
        reached_ancestry_table = False
        for line in structure_file:
            if line.strip() == STRUCTURE_ANCESTRY_HEADER:
                reached_ancestry_table = True
            elif reached_ancestry_table and line.strip() == '':
                break
            elif reached_ancestry_table and line.strip()[:5] != 'Label':
                clumpp_file.write(line)
        clumpp_file.write('\n')


def generate_structure_file(vcf_filename, selected_snps, indvs_pops,
                            replicate):
    """ Convert VCF to STRUCTURE format - output only selected SNPs """
    # Initialise dict in which to store genotypes
    structure_genotypes = init_genotype_dict(indvs_pops)

    # Iterate through file and save genotypes to dict
    vcf_reader = vcf.Reader(open(vcf_filename, 'r'))
    for record in vcf_reader:
        current_snp = '{0}_{1}'.format(record.CHROM, record.POS)
        # Only output selected SNPs
        if current_snp in selected_snps[replicate]:
            for indiv in indvs_pops:
                # Convert genotype to STRUCTURE format
                genotype = record.genotype(indiv).gt_bases
                genotype1, genotype2 = get_structure_genotype(genotype)
                # Store the two alleles in two separate lists
                structure_genotypes[indiv][0].append(genotype1)
                structure_genotypes[indiv][1].append(genotype2)

    # Output genotypes for each individual
    structure_file = open(replicate, 'w')
    pops = []
    indiv_count = 1
    for indiv in sorted(indvs_pops.keys()):
        # Obtain unique integer representative for each pop
        if indvs_pops[indiv] not in pops:
            pops.append(indvs_pops[indiv])
        pop = pops.index(indvs_pops[indiv]) + 1
        concat_genotypes1 = ' '.join(structure_genotypes[indiv][0])
        concat_genotypes2 = ' '.join(structure_genotypes[indiv][1])
        line1 = '{0}\t{1}\t{2}\n'.format(indiv_count, pop, concat_genotypes1)
        line2 = '{0}\t{1}\t{2}\n'.format(indiv_count, pop, concat_genotypes2)
        structure_file.write(line1)
        structure_file.write(line2)
        indiv_count += 1
    structure_file.close()
    # Return structure filename
    return replicate


def calc_clust_stats(clumpp_prefix, replicates):
    """ Calculate cluster stats using Puechmaille 2016 method """
    mean_thresholds = []
    median_thresholds = []
    for x in range(1, int(replicates) + 1):
        # Iterate through CSV and store cluster assignments in dict/dict/list
        clumpp_permfilename = '{0}_perm_{1}.csv'.format(clumpp_prefix, x)
        clumpp_permfile = open(clumpp_permfilename, 'r')
        cluster_assign = {}
        for line in clumpp_permfile:
            cols = line.split(',')
            pop = cols[1]
            for cluster_number, cluster_value in enumerate(cols[2:]):
                # Add cluster and pop to dict if not already present
                if cluster_number not in cluster_assign:
                    cluster_assign[cluster_number] = {}
                if pop not in cluster_assign[cluster_number]:
                    cluster_assign[cluster_number][pop] = []
                # Store values for each individual in list
                cluster_assign[cluster_number][
                    pop].append(float(cluster_value))

        # Iterate through each cluster and each pop to calculate mean/median
        cluster_mean = {}
        cluster_median = {}
        for cluster_number in cluster_assign:
            # Obtain mean/median for all values in each pop
            cluster_mean[cluster_number] = []
            cluster_median[cluster_number] = []
            for pop in cluster_assign[cluster_number]:
                mean = numpy.mean(cluster_assign[cluster_number][pop])
                cluster_mean[cluster_number].append(mean)
                median = numpy.median(cluster_assign[cluster_number][pop])
                cluster_median[cluster_number].append(mean)

        # Obtain max value for each cluster and evaluate how many >0.5
        mean_threshold_count = 0
        median_threshold_count = 0
        for cluster_number in cluster_mean:
            # Maximum of mean values
            max_mean = numpy.max(cluster_mean[cluster_number])
            if max_mean > 0.5:
                mean_threshold_count += 1
            # Maximum of median values
            max_median = numpy.max(cluster_median[cluster_number])
            if max_median > 0.5:
                median_threshold_count += 1
        mean_thresholds.append(mean_threshold_count)
        median_thresholds.append(median_threshold_count)

    # Calculate MedMeaK and MaxMeaK
    MedMeaK = numpy.median(mean_thresholds)
    MaxMeaK = numpy.max(mean_thresholds)

    # Calculate MedMedK and MaxMedK
    MedMedK = numpy.median(mean_thresholds)
    MaxMedK = numpy.max(mean_thresholds)
    return MedMeaK, MaxMeaK, MedMedK, MaxMedK


def generate_identifiers(output_folder, vcf_filename, replicates):
    """ Generate timestamp identifiers for replicates/filenames """
    id_list = []
    timestamp = int(time.time())
    filename = vcf_filename.replace('.vcf', '')
    for x in range(0, replicates):
        id_number = timestamp + x
        identifier = '{0}/{1}_{2}.str'.format(output_folder, filename,
                                              id_number)
        id_list.append(identifier)
    return id_list


def generate_batches(replicate_IDs, threads):
    """ Generate list with batches of n-threads """
    batches = []
    temp_batch = []
    total_count = count = 0
    for replicate in replicate_IDs:
        count += 1
        total_count += 1
        temp_batch.append(replicate)
        if count == threads or total_count == len(replicate_IDs):
            batches.append(temp_batch)
            temp_batch = []
            count = 0
    return batches


def parallelise(function, batches):
    """ Run function in parallel (running batches of replicates) """
    for batch in batches:
        pool = Pool(processes=len(batches[0]))
        pool.map(function, batch)
        pool.close()
        pool.join()
        print_msg('{0} reps DONE'.format(len(batch)))


def main(vcf_filename, pop_filename, maxK, replicates, threads):
    # Create output directory and logfile
    output_folder = init_output_folder(vcf_filename)

    # Initialise dict of indvs (keys) their and pop assignments (values)
    print_msg('Initialise individuals and populations...\n')
    indvs_pops = dict_from_popfile(pop_filename)
    num_indvs = len(indvs_pops)

    # Generate identifiers for reps and organise in batches of n-threads
    replicate_IDs = generate_identifiers(output_folder, vcf_filename,
                                         int(replicates))
    batches = generate_batches(replicate_IDs, int(threads))
    num_reps = len(replicate_IDs)

    # Create dict with random subsampled snps for each replicate
    # TODO: PRINT WHICH SNPS WERE USED FOR EACH RUN
    print_msg('Subsample SNPs (one random SNP per locus)... ')
    selected_snps = select_random_snps(vcf_filename, replicate_IDs)
    num_loci = len(selected_snps[replicate_IDs[0]])
    print_msg('[{0} SNPs/loci]\n'.format(num_loci))

    # Generate STRUCTURE files for each replicate
    print_msg('Outputting {0} STRUCTURE files...'.format(replicates))
    function_partial = partial(generate_structure_file, vcf_filename,
                               selected_snps, indvs_pops)
    parallelise(function_partial, batches)

    # TODO: INIT STRUCTURE mainparams file

    # Run all replicates through STRUCTURE (REF)
    for K in range(2, int(maxK) + 1):
        print_msg('\nExecuting {0} parallel STRUCTURE runs '
                  'for K = {1} ...'.format(threads, K))
        function_partial = partial(run_structure, num_indvs, num_loci, K)
        parallelise(function_partial, batches)

    # Summarise files with CLUMPP for each K (Jakobsson and Rosenberg 2007)
    for K in range(2, int(maxK) + 1):
        print_msg('\nRunning CLUMPP on replicates for K = {0} ...'.format(K))
        # Initialise CLUMPP paramfile
        clumpp_prefix = '{0}/clumpp_K{1}'.format(output_folder, K)
        paramfile = create_clumpp_paramfile(clumpp_prefix, K,
                                            num_indvs, num_reps)
        # Generate CLUMPP input
        generate_clumpp_input(clumpp_prefix, replicate_IDs, K)
        # Run CLUMPP
        run_clumpp(paramfile)
        # Convert to CSV
        convert_clumpp_to_csv(clumpp_prefix, indvs_pops, num_reps)

    # Detect the number of clusters (Puechmaille 2016)
    for K in range(2, int(maxK) + 1):
        clumpp_prefix = '{0}/clumpp_K{1}'.format(output_folder, K)
        MedMeaK, MaxMeaK, MedMedK, MaxMedK = calc_clust_stats(clumpp_prefix,
                                                              replicates)
        print_msg('\nK = {0}: MedMeaK {1} MaxMeaK {2} MedMedK {3} '
                  'MaxMedK {4}'.format(K, MedMeaK, MaxMeaK, MedMedK, MaxMedK))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('vcf_filename', metavar='vcf_file',
                        help='input file with SNP data (`.vcf`)')
    parser.add_argument('pop_filename', metavar='pop_file',
                        help='population file (.txt)')
    parser.add_argument('maxK', metavar='maxK',
                        help='maximum number of K (expected clusters)')
    parser.add_argument('replicates', metavar='replicates',
                        help='number of replicate runs for each K')
    parser.add_argument('threads', metavar='threads',
                        help='number of parallel threads')
    args = parser.parse_args()
    main(args.vcf_filename, args.pop_filename, args.maxK, args.replicates,
         args.threads)