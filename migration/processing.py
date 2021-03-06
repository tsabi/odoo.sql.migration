import csv
import logging
import os
import shutil
from os.path import basename, join, splitext
from collections import namedtuple
from multiprocessing import Pool

from .sql_commands import upsert, setup_temp_table

HERE = os.path.dirname(__file__)
logging.basicConfig(level=logging.DEBUG)
LOG = logging.getLogger(basename(__file__))

# increase the maximum csv field size. Hardcode it for the time being
# See https://bitbucket.org/anybox/anybox.migration.openerp/issue/2/
csv.field_size_limit(20971520)


class CSVProcessor(object):
    """ Take a csv file, process it with the mapping
    and output a new csv file
    """
    def __init__(self, mapping, fk2update=None):

        self.fk2update = fk2update or {}  # foreign keys to update during postprocessing
        self.mapping = mapping  # mapping.Mapping instance
        self.target_columns = {}
        self.writers = {}
        self.updated_values = {}
        self.fk_mapping = {}  # mapping for foreign keys
        self.ref_mapping = {}  # mapping for references
        self.lines = 0
        self.is_moved = {}
        self.existing_target_records = {}
        self.existing_m2m_records = {}
        self.filtered_columns = {}
        self.existing_target_columns = []

    def get_target_columns(self, filepaths, forget_missing=False, target_connection=None):
        """ Compute target columns with source columns + mapping
        """
        if self.target_columns:
            return self.target_columns

        get_targets = self.mapping.get_target_column
        for filepath in filepaths:
            source_table = basename(filepath).rsplit('.', 1)[0]
            with open(filepath) as f:
                source_columns = csv.reader(f).next()
            for source_column in source_columns + ['_']:

                mapping = get_targets(source_table, source_column)
                # no mapping found, we warn the user
                if mapping is None:
                    origin = source_table + '.' + source_column
                    LOG.warn('No mapping definition found for column %s', origin)
                    continue
                if mapping == '__copy__':
                    continue
                elif mapping in (False, '__forget__'):
                    continue
                else:
                    for target in mapping:
                        t, c = target.split('.')
                        self.target_columns.setdefault(t, set()).add(c)
        self.target_columns = {k: sorted([c for c in v if c != '_'])
                               for k, v in self.target_columns.items()}

        if forget_missing and target_connection:
            self.existing_target_columns = self.get_explicit_columns(self.target_columns.keys(), target_connection)
            for table, cols in self.target_columns.items():
                LOG.warn('The following columns will be auto forgotten in table %s', table)
                LOG.warn(','.join(set(cols).difference(self.existing_target_columns[table])))
                self.target_columns[table] = [c for c in cols if c in self.existing_target_columns[table]]
        return self.target_columns

    def get_explicit_columns(self, target_tables, target_connection):
        """
        If autoforget is enabled we need to explicitly specify
        which columns exist in target
        """
        res = {}
        for target_table in target_tables:
            with target_connection.cursor() as c:
                c.execute("SELECT * FROM {} LIMIT 0".format(target_table))
                res[target_table] = [
                    desc[0] for desc in c.description]
        return res


    def set_existing_data(self, existing_records):
        """let the existing data be accessible during processing
        """
        self.existing_target_records = existing_records
        # the same without ids
        # WTF?
        self.existing_m2m_records = {
            table: [{k: str(v) for k, v in nt.iteritems() if k != 'id'} for nt in existing]
            for table, existing in existing_records.iteritems()
        }

    def reorder_with_discriminators(self, tables):
        """ Reorder the filepaths based on tables pointed by discriminators
        (if they are fk)
        """
        # get the list of tables pointed by discriminators
        discriminator_tables = set()
        for table, columns in self.mapping.discriminators.iteritems():
            for column in columns:
                field = table + '.' + column
                if field in self.fk2update:
                    discriminator_tables.add(self.fk2update[field])
        #special handling to ensure ir.property is last
        if 'ir_property' in tables:
            discriminator_tables.add('ir_property')
        # remove them from the initial tables
        tables = [t for t in tables if t not in discriminator_tables]
        # reorder the small set with a very basic algorithm:
        # put at left those without fk discriminator, right others
        ordered_tables = []
        for table in discriminator_tables:
            if table == 'ir_property':
                tables.append(table)
                continue
            for column in self.mapping.discriminators.get(table, ()):
                field = table + '.' + column
                if table in ordered_tables:
                    continue
                if field in self.fk2update:
                    ordered_tables.append(table)
                else:
                    ordered_tables.insert(0, table)
        # append the two lists
        ordered_tables += tables
        return ordered_tables

    def process(self, source_dir, source_filenames, target_dir,
                target_connection=None, del_csv=False):
        """ The main processing method
        """
        # compute the target columns
        filepaths = [join(source_dir, source_filename) for source_filename in source_filenames]
        source_tables = [splitext(basename(path))[0] for path in filepaths]
        self.target_columns = self.get_target_columns(filepaths)
        # load discriminator values for target tables
        # TODO

        # filenames and files
        target_filenames = {
            table: join(target_dir, table + '.target.csv')
            for table in self.target_columns
        }
        target_files = {
            table: open(filename, 'ab')
            for table, filename in target_filenames.items()
        }
        self.writers = {t: csv.DictWriter(f, self.target_columns[t], delimiter=',')
                        for t, f in target_files.items()}
        for writer in self.writers.values():
            writer.writeheader()

        # update filenames and files
        update_filenames = {
            table: join(target_dir, table + '.update.csv')
            for table in self.target_columns
        }
        update_files = {
            table: open(filename, 'ab')
            for table, filename in update_filenames.items()
        }
        self.updatewriters = {t: csv.DictWriter(f, self.target_columns[t], delimiter=',')
                              for t, f in update_files.items()}
        for writer in self.updatewriters.values():
            writer.writeheader()
        LOG.info(u"Processing CSV files...")
        # We should first reorder the processing so that tables pointed to by
        # discriminator values which are fk be processed first. This is not the
        # most common case, but otherwise offsetting these values may fail,
        # leading to unwanted matching and unwanted merge.
        ordered_tables = self.reorder_with_discriminators(source_tables)
        ordered_paths = [join(source_dir, table + '.csv') for table in ordered_tables]
        for source_filepath in ordered_paths:
            self.process_one(source_filepath, target_connection)

        #Delete Files to free up RAM on tmpfs
        if del_csv:
            try:
                map(os.remove, ordered_paths)
            except os.error:
                LOG.warning(u"Couldn't remove CSV Files")
        # close files
        [f.close() for f in target_files.values() + update_files.values()]

        # POSTPROCESS target filenames and files
        target2_filenames = {
            table: join(target_dir, table + '.target2.csv')
            for table in self.target_columns
        }
        target2_files = {
            table: open(filename, 'ab')
            for table, filename in target2_filenames.items()
        }
        self.writers = {t: csv.DictWriter(f, self.target_columns[t], delimiter=',')
                        for t, f in target2_files.items()}
        for writer in self.writers.values():
            writer.writeheader()
        LOG.info(u"Postprocessing CSV files...")
        for filename in target_filenames.values():
            filepath = join(target_dir, filename)
            self.postprocess_one(filepath)

        # Delete files to free up RAM on tmpfs
        if del_csv:
            try:
                map(os.remove, target_filenames.values())
            except os.error:
                LOG.warning(u"Couldn't remove target CSV Files")

        [f.close() for f in target2_files.values()]

        # POSTPROCESS update filenames and files
        update2_filenames = {
            table: join(target_dir, table + '.update2.csv')
            for table in self.target_columns
        }
        update2_files = {
            table: open(filename, 'ab')
            for table, filename in update2_filenames.items()
        }
        self.writers = {t: csv.DictWriter(f, self.target_columns[t], delimiter=',')
                        for t, f in update2_files.items()}
        for writer in self.writers.values():
            writer.writeheader()
        for filename in update_filenames.values():
            filepath = join(target_dir, filename)
            self.postprocess_one(filepath)
        # close files
        for f in update2_files.values():
            f.close()

    def process_one(self, source_filepath,
                    target_connection=None):
        """ Process one csv file
        The fk_mapping should not be read in this method. Only during postprocessing,
        Because the processing order is not determined (unordered dicts)
        """
        #process one gets a single filepath - good for map
        #processing needs to go through a number of steps

        source_table = basename(source_filepath).rsplit('.', 1)[0]
        get_targets = self.mapping.get_target_column

        # here we process the source csv
        with open(source_filepath, 'rb') as source_csv: #we start with the raw data and open it
            reader = csv.DictReader(source_csv, delimiter=',')
            # process each csv line
            for source_row in reader: # then iterate the rows
                self.lines += 1
                target_rows = {}
                # process each column (also handle '_' as a possible new column)
                source_row.update({'_': None})
                for source_column in source_row: # dict like {'id': 1, 'create_uid': 1, 'name': 'gay'}
                    mapping = get_targets(source_table, source_column)
                    if mapping is None: # if the column isn't mapped we forget about it
                        continue
                    # we found a mapping, use it
                    # for every record we must first process any columns functions to transform the data
                    for target_record, function in mapping.items(): # This holds what we need to do
                        target_table, target_column = target_record.split('.')
                        target_rows.setdefault(target_table, {}) # create a dictionary for the rows using the table as key

                        # We deal with forgotten columns here
                        if self.existing_target_columns and (
                                    target_column not in
                                    self.existing_target_columns[target_table]):
                            continue

                        if target_column == '_': # if its a new column to feed we move on, this makes no sense?
                            continue
                        #next we handle special indicators
                        if function in (None, '__copy__'): # copy it
                            # mapping is None: use identity
                            target_rows[target_table][target_column] = source_row[source_column]
                        elif type(function) is str and function.startswith('__ref__'): # use another field
                            target_rows[target_table][target_column] = source_row[source_column]
                            model_column = function.split()[1]
                            self.ref_mapping[target_record] = model_column # what? for every row, no way, this can be done way earlier

                        elif function in (False, '__forget__'): # for this column we forget about it - obvious - but why do we need to do it every time?
                            # mapping is False: remove the target column
                            del target_rows[target_table][target_column]
                        # in case the id has moved to a new record,
                        # we should save the mapping to correctly fix fks
                        # This can happen in case of semantic change like res.partner.address
                        elif function == '__moved__':
                            source_table not in self.is_moved and self.is_moved.update({source_table: target_table}) # store that it has moved
                            newid = self.mapping.newid(target_table) # give it a new database_id
                            target_rows[target_table][target_column] = newid
                            self.fk_mapping.setdefault(source_table, {})
                            self.fk_mapping[source_table][int(source_row[source_column])] = newid + self.mapping.max_target_id[target_table] # so fk_mapping looks like {'mail_alias': {1: 100}
                        else:
                            # mapping is supposed to be a function
                            result = function(self, source_row, target_rows) #run the compiled function
                            if result == '__forget_row__':
                                target_rows[target_table]['__forget_row__'] = True
                            target_rows[target_table][target_column] = result

                # now our target_row is set write it
                # offset all ids except existing data and choose to write now or update later
                # forget any rows that are being filtered
                for table, target_row in target_rows.items():
                    if '__forget_row__' in target_row:
                        continue
                    if not any(target_row.values()):
                        continue
                    # here we handle collisions with existing data
                    discriminators = self.mapping.discriminators.get(table) # list of field names
                    # if the line exists in the target db, we don't offset and write to update file
                    # (we recognize by matching the dict of discriminator values against existing)
                    existing = self.existing_target_records.get(table, []) #get all the existing db_values that have an id field (see exporting.py)
                    existing_without_id = self.existing_m2m_records.get(table, []) #get m2m fields
                    match_values = {d: target_row[d] for d in (discriminators or [])} # a dict with column: write

                    # before matching existing, we should fix the discriminator_values which are fk
                    # FIXME refactor and merge with the code in postprocess
                    # GG Move this, only concerned with foreign keys
                    for column, value in match_values.items():
                        fk_table = self.fk2update.get(table + '.' + column)
                        if value and fk_table:
                            value = int(value)
                            # this is BROKEN because it needs the fk_table to be processed before.
                            if value in self.fk_mapping.get(fk_table, []):
                                match_values[column] = str(
                                    self.fk_mapping[fk_table].get(value, value))

                    # save the mapping between source id and existing id

                    if (discriminators
                            and 'id' in target_row
                            and all(match_values.values())
                            and match_values in existing_without_id):
                        # find the id of the existing record in the
                        # target
                        for i, nt in enumerate(existing):
                            if match_values == {k: str(v)
                                                        for k, v in nt.iteritems() if k != 'id'}:
                                existing_id = existing[i]['id']
                                break
                        self.fk_mapping.setdefault(table, {})
                            # we save the match between source and existing id
                            # to be able to update the fks in the 2nd pass
                        self.fk_mapping[table][int(target_row['id'])] = existing_id

                    # fix fk to a moved table with existing data
                        if source_table in self.is_moved:
                            source_id = int(source_row['id'])
                            if source_id in self.fk_mapping[source_table]:
                                target_row['id'] = existing_id
                                self.fk_mapping[source_table][source_id] = existing_id

                        self.updatewriters[table].writerow(target_row)
                    else:
                        # offset the id of the line, except for m2m (no id)
                        if 'id' in target_row:

                            target_row['id'] = str(int(target_row['id']) + self.mapping.max_target_id[table])
                            # handle deferred records
                            if table in self.mapping.deferred:
                                upd_row = {k: v for k, v in target_row.iteritems()
                                           if k == 'id'
                                           or (k in self.mapping.deferred[table] and v != '')}
                                if len(upd_row) > 1:
                                    self.updatewriters[table].writerow(upd_row)
                                for k in self.mapping.deferred[table]:
                                    if k in target_row:
                                        del target_row[k]
                        # don't write incomplete m2m
                        if ('id' not in target_row
                                and len(target_row) == 2
                                and not all(target_row.values())):
                            continue
                        # otherwise write the target csv line
                        self.writers[table].writerow(target_row)

    def postprocess_one(self, target_filepath):
        """ Postprocess one target csv file
        """
        table = basename(target_filepath).rsplit('.', 2)[0]
        with open(target_filepath, 'rb') as target_csv:
            reader = csv.DictReader(target_csv, delimiter=',')
            for target_row in reader:
                write = True
                postprocessed_row = {}
                # fix the foreign keys of the line
                for key, value in target_row.items():
                    target_record = table + '.' + key
                    postprocessed_row[key] = value
                    fk_table = self.fk2update.get(target_record)
                    # if this is a fk, fix it
                    if value and fk_table:
                        # if the target record is an existing record it should be in the fk_mapping
                        # so we restore the real target id, or offset it if not found
                        target_table = self.is_moved.get(fk_table, fk_table)
                        value = int(value)
                        postprocessed_row[key] = self.fk_mapping.get(fk_table, {}).get(
                            value, value + self.mapping.max_target_id[target_table])
                    # if we're postprocessing an update we should restore the id as well, but only if it is an update
                    if key == 'id' and table in self.fk_mapping and 'update' in target_filepath:
                        value = int(value)
                        postprocessed_row[key] = self.fk_mapping[table].get(value, value)
                    if value and target_record in self.ref_mapping:  # manage __ref__
                        # first find the target table of the reference
                        ref_column = self.ref_mapping[target_record]
                        if ref_column == key: # like ir_property
                            ref_table, fk_value = value.split(',')
                            fk_id = int(fk_value)
                            ref_table = ref_table.replace('.', '_')
                            try:
                                new_fk_id = self.fk_mapping.get(ref_table, {}).get(
                                    fk_id, fk_id + self.mapping.max_target_id[ref_table])
                            except KeyError:
                                write = False
                            postprocessed_row[key] = value.replace(fk_value, str(new_fk_id))
                        else:
                            value = int(value)
                            ref_table = target_row[ref_column].replace('.', '_')
                            try:
                                postprocessed_row[key] = self.fk_mapping.get(ref_table, {}).get(
                                    value, value + self.mapping.max_target_id.get(ref_table, 0))
                            except KeyError:
                                print u'Key %s\nTable %s\n' % (key, ref_table)
                                print target_row
                                raise


                # don't write m2m lines if they exist in the target
                # FIXME: refactor these 4 lines with those from process_one()?
                discriminators = self.mapping.discriminators.get(table)
                existing_without_id = self.existing_m2m_records.get(table, [])
                existing = self.existing_target_records.get(table)
                discriminator_values = {d: str(postprocessed_row[d])
                                        for d in (discriminators or [])}
                if (write and
                        ('id' in postprocessed_row or
                         discriminator_values not in existing_without_id)):
                    self.writers[table].writerow(postprocessed_row)

    @staticmethod
    def update_all(filepaths, connection, suffix=""):
        """ Apply updates in the target db with update file
        """

        to_update = []
        upd_record = namedtuple('upd_record', 'path, target, suffix, cols, pkey')
        for filepath in filepaths:
            target_table = basename(filepath).rsplit('.', 2)[0]
            temp_table = target_table + suffix
            with open(filepath, 'rb') as update_csv, connection.cursor() as c:
                reader = csv.DictReader(update_csv, delimiter=',')
                for x in reader: # lame way to check if it has lines - Note: try while reader:
                    update_csv.seek(0)
                    pkey = setup_temp_table(c, target_table, suffix=suffix)
                    if not pkey:
                        LOG.error(u'Can\'t update data without primary key')
                    else:
                        columns = ','.join(["{0}=COALESCE({2}.{0}, {1}.{0})".format(col, target_table, temp_table)
                                         for col in csv.reader(update_csv).next() if col != pkey])
                        to_update.append(upd_record(filepath, target_table, suffix, columns, pkey))
                    break
        if to_update:
            upsert(to_update, connection)
        else:
            LOG.info(u'Nothing to update')

    def drop_stored_columns(self, connection):
        for table, columns in self.mapping.stored_fields.items():
            # potentially unsafe
            query = ','.join(['DROP COLUMN %s CASCADE' % f for f in columns])
            with connection.cursor() as c:
                c.execute('ALTER TABLE %s %s' % (table, query))
