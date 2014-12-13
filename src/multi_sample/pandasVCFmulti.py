import os,sys,gzip
import pandas as pd
from variantAnnotations import *
from Vcf_metadata import *
import numpy as np
import gc


class Vcf(object):
    '''
        Loads in a vcf file, aware of gzipped files.
        testing
        '''
    
    def __init__(self, filename, sample_id='', cols='', chunksize=5000):
        
        #Header
        header_parsed = Vcf_metadata(filename)
        self.header_df = self.get_header_df(header_parsed.header)  #header parsed into key/values dataframe
        
        
        
        #Sample IDs
        self.samples = list(self.header_df.ix['SampleIDs'])[0]
        if sample_id == 'all':
            self.sample_id = self.samples[:]
            #self.sample_id.remove('')
        else:
            self.sample_id = [sample_id]
        
        
        #Columns
        self.all_columns = list(self.header_df.ix['ColumnHeader'])[0]
        self.FORMAT = self.all_columns[8]
        
        
        assert len(set(cols) & set(['#CHROM', 'POS', 'REF', 'ALT', 'FORMAT'])), "cols requires the following columns: ['#CHROM', 'POS', 'REF', 'ALT', 'FORMAT']"
        self.cols = cols
        if len(cols) > 0:
            self.usecols = [c for c in self.all_columns if c in cols]
            if len(sample_id) > 0:
                self.usecols.extend(self.sample_id)
            else: 
                assert False, 'no sample IDs'
        else:
            self.usecols = [s for s in self.cols if s not in self.samples]
            self.usecols.extend(self.sample_id)
        
        #Open pandas chunk object (TextReader)
        self.chunksize = chunksize
        self.vcf_chunks = pd.read_table(filename, sep="\t", compression=header_parsed.compression, skiprows=(len(self.header_df)-2), usecols=self.usecols, chunksize=chunksize)
    
    
    
    
    def get_header_df(self, header_txt):
        '''
            Parses header into pandas DataFrame
            '''
        key_value_header = [i.replace('##','').replace('\n','').split('=',1) for i in header_txt if '##' in i]
        key_value_header.append(['SampleIDs',header_txt[-1].rstrip('\n').split('\t')[9:]])
        key_value_header.append(['ColumnHeader', header_txt[-1].rstrip('\n').split('\t')])
        header_df =  pd.DataFrame.from_records(key_value_header)
        header_df.set_index(0,inplace=True)
        header_df.index.name = 'header_keys'
        header_df.columns = ['header_values']
        return header_df
    
    
    
    
    def get_vcf_df_chunk(self):
        '''
            This function iterates through the VCF files using the user-defined
            chunksize (default = 5000 lines).
            '''
        
        self.df = self.vcf_chunks.get_chunk()
        self.df.drop_duplicates(inplace=True)
        self.df.columns = [c.replace('#', '') for c in self.usecols]
        self.df.set_index(['CHROM', 'POS', 'REF', 'ALT'], inplace=True, drop=False)
        
        self.df_bytes = self.df.values.nbytes + self.df.index.nbytes + self.df.columns.nbytes
        
        return 0
    
    
    def drop_hom_ref_df(self, df):
        
        #recording number of homozygous reference calls for each variant
        
        df_hom_ref = df[df['sample_genotypes'].isin(['0|0','0/0'])]  #dropping all homozygous reference
        df = df[~df.index.isin(df_hom_ref.index)]
        
        df_hom_ref['count'] = 1
        hom_ref_counts = df_hom_ref.groupby(level=[0,1,2,3])['count'].aggregate(np.sum)
        hom_ref_counts.name = 'hom_ref_counts'
        
        
        df.reset_index(level=4, inplace=True, drop=True)
        df = df.join(hom_ref_counts, how='left')
        df['hom_ref_counts'].fillna(value=0, inplace=True)
        df.set_index(['CHROM', 'POS', 'REF', 'ALT', 'sample_ids'], inplace=True, drop=False)
        return df
    
    
    
    def add_variant_annotations(self, split_columns='', verbose=False, inplace=False, drop_hom_ref=True):
        
        '''
            This function adds the following annotations for each variant:
            multiallele, phase, a1, a2, GT1, GT2, vartype1, vartype2, zygosity,
            and parsed FORMAT values, see below for additional information.
            
            Parameters
            --------------
            
            split_columns: dict, optional
            key:FORMAT id value:#fields expected
            e.g. {'AD':2} indicates Allelic Depth should be
            split into 2 columns.
            
            verbose: bool, default=False
                This will describe how many missing variants were dropped
            
            inplace: bool, default=False
                This will replace the sample_id column with parsed columns,
                and drop the FORMAT field.  If True, this will create an
                additional dataframe, df_annot, to the Vcf object composed of
                the parsed columns (memory intensive)
            
            drop_hom_ref: bool, default=True
                If True this will count homozygous reference genotype calls for each
                variant, add these counts as a df column and drop all hom-ref
                variant calls from the df_annot dataframe.  10X faster than not
                dropping if large multi-sample vcf, e.g. 1000genomes
            
            
            
            Output
            --------------
            This function adds the following annotations to each variant:
            
            multiallele: {0,1} 0=biallele  1=multiallelic
            
            phase: {'/', '|'} /=unphased, |=phased
            
            a1: DNA base representation of allele1 call, e.g. A
            a2: DNA base representation of allele2 call, e.g. A
            
            GT1: numeric representation of allele1 call, e.g. 0
            GT2: numeric representation of allele2 call, e.g. 1
            
            vartype1: {snp, mnp, ins, del, indel or SV} variant type of first allele
            vartype2: {snp, mnp, ins, del, indel or SV} variant type of second allele
            
            zygosity: {het-ref, hom-ref, alt-ref, het-miss, hom-miss}
            
            FORMAT values: any values associated with the genotype calls are
            added as additional columns, split_columns are further
            split by ',' into individual columns
            
            
            '''
        
        
        
        self.df_groups = self.df.groupby('FORMAT')
        
        parsed_df = []
        for i,df_format in self.df_groups: #iterating through FORMAT groups
            
            df_format = df_format[df_format['ALT'] != '.']
            df_format = df_format[self.sample_id]
            df_format = df_format.replace(to_replace='.', value=np.NaN)
            df_format = pd.DataFrame( df_format.stack(), columns=['sample_genotypes'] )
            df_format.index.names = ['CHROM', 'POS', 'REF', 'ALT', 'sample_ids']
            df_format.reset_index(inplace=True)
            df_format.set_index(['CHROM', 'POS', 'REF', 'ALT', 'sample_ids'], drop=False, inplace=True)
            df_format['FORMAT'] = i
            df_format.drop_duplicates(inplace=True)
            if drop_hom_ref:
                df_format = self.drop_hom_ref_df(df_format)
            parsed_df.append( get_vcf_annotations(df_format, 'sample_genotypes', split_columns=split_columns) )
            #parsed_df.append( df_format )
        
        del self.df_groups
        gc.collect()
        
        if inplace:
            self.df = pd.concat(parsed_df)
        else:
            self.df_annot = pd.concat(parsed_df)
        
        
        
        return 0
        
        
#        if set(self.sample_id) - set(self.df.columns) > 0:
#            print 'Sample genotype column not found, add_variant_annotations can only be called if the FORMAT and sample genotype columns are available?'
#            return 1
#        
#        if verbose:
#            print 'input variant rows:', len(self.df)
#        
#        self.df.drop_duplicates(inplace=True)
#        
#        var_counts = len(self.df)
#        
#        self.df = self.df[self.df[self.sample_id]!='.']  #dropping missing genotype calls
#        
#        if verbose:
#            
#            print 'dropping',var_counts - len(self.df), 'variants with genotype call == "." '
#            print 'current variant rows:', len(self.df)
#        
#        
#        if inplace:
#            if 'POS' not in self.df.columns:
#                self.df.reset_index(inplace=True)
#                self.df.set_index(['CHROM', 'POS', 'REF', 'ALT'],inplace=True, drop=False)
#            self.df = get_vcf_annotations(self.df, self.sample_id, split_columns)
#        else:
#            self.df_annot = get_vcf_annotations(self.df, self.sample_id, split_columns)
#            self.df.set_index(['CHROM', 'POS', 'REF', 'ALT'],inplace=True)
#        
#        return 0


