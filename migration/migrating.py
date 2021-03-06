import sys
import time

import shutil
import argparse
from ConfigParser import SafeConfigParser

from tempfile import mkdtemp
from .exporting import export_to_csv, extract_existing
from .importing import import_from_csv
from .mapping import Mapping
from .processing import CSVProcessor
from .depending import add_related_tables
from .depending import get_fk_to_update
from .sql_commands import drop_constraints, get_management_connection, get_db_connection, create_new_db, kill_db_connections

import logging
from os.path import basename, join, abspath, dirname, exists, normpath
from os import listdir

HERE = dirname(__file__)
logging.basicConfig(level=logging.DEBUG)
LOG = logging.getLogger(basename(__file__))


def main():
    """ Main console script
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config',
                        default='.last.cfg',
                        help=u'List of mapping files. '
                             u'If not found in the specified path, '
                             u'each file is searched in the "mappings" '
                             u'dir of this tool. '
                             u'Example: openerp6.1-openerp7.0.yml custom.yml',
                        nargs='+'
                        )
    parser.add_argument('-l', '--list',
                        action='store_true',
                        default=False,
                        help=u'List provided mappings')
    parser.add_argument('-s', '--source',
                        default='test',
                        help=u'Source db')
    parser.add_argument('-t', '--target',
                        help=u'Target db')
    parser.add_argument('-k', '--keepcsv',
                        action='store_true',
                        help=u'Keep csv files in the current directory')
    parser.add_argument('-r', '--relation',
                        nargs='+',
                        help=u'List of space-separated tables to migrate. '
                        'Example : res_partner res_users')
    parser.add_argument('-x', '--excluded',
                        nargs='+',
                        help=u'List of space-separated tables to exclude'
                        )
    parser.add_argument('-p', '--path',
                        default='openerp6.1-openerp7.0.yml',
                        help=u'List of mapping files. '
                             u'If not found in the specified path, '
                             u'each file is searched in the "mappings" '
                             u'dir of this tool. '
                             u'Example: openerp6.1-openerp7.0.yml custom.yml',
                        nargs='+'
                        )
    parser.add_argument('-w', '--write',
                        action='store_true', default=False,
                        help=u'Really write to the target database if '
                             u'migration is successful.'
                        )
    parser.add_argument('-n', '--newdb',
                        help=u'Create a new database based on target. '
                             u'Existing db of same name will be dropped')
    parser.add_argument('-o', '--owner',
                        help=u'Owner of newly created database '
                             u'(must already exist).')
    parser.add_argument('-f', '--dropfk',
                        action='store_true', default=False,
                        help=u'Drops foreign key constraints on tables '
                             u'and adds back after import.'
                             u' Must be used with --newdb or -n')
    parser.add_argument('-q', '--quick',
                        action='store_true', default=False,
                        help=u'Turns it up to 11. '
                             u'Drops foreign key constraints on tables '
                             u'and adds back after import.'
                             u' Auto creates new database if -n not specified')
    parser.add_argument('--tmpfs',
                        action='store_true', default=False,
                        help=u'Uses tmpfs for csv\'s '
                             u'(Ubuntu / RHEL and variants')
    parser.add_argument('--forgetmissing',
                        action='store_true', default=False,
                        help=u'Will automatically drop columns '
                             u'not in the target database')


    args = parser.parse_args()

    source_db, target_db, relation = args.source, args.target, args.relation
    mapping_names = args.path if type(args.path) is list else [args.path]
    excluded = args.excluded or [] + [
        'ir_model'
    ]
    if args.list:
        print(u'\n'.join(listdir(join(HERE, 'mappings'))))
        sys.exit(0)

    if not all([source_db, target_db, relation]):
        print(u'Please provide at least -s, -t and -r options')
        sys.exit(1)

    if args.tmpfs:
        print(u'To preserve memory CSV files will be removed during processing')
        if args.keepcsv:
            print(u"Specifying --keepcsv doesn't work with --tmpfs")
            args.keepcsv = False
    elif args.keepcsv:
        print(u"Writing CSV files in the current dir")

    identifier = str(int(time.time()))[-4:]

    if args.quick:
        args.dropfk = True

    if args.dropfk and not (args.newdb or args.quick):
        print(u'Due to the dangers of being unable to roll back if an error occurs\n'
              u'and the dangers of not correctly recording constraints this option\n'
              u'is only valid with the -n flag')
        sys.exit(1)

    temppath = abspath(args.tmpfs and '/dev/shm' or '.')
    tempdir = mkdtemp(prefix=source_db + '_' + identifier + '_',
                      dir=temppath)

    identifier = basename(normpath(tempdir))
    if args.quick and not args.newdb:
        args.newdb = identifier.lower()
    print(u'The identifier for this migration is "{0}"\n'
          u'The database will be "{1}"'.format(
          identifier, args.write and args.newdb or (args.write and target_db or 'left alone')))
    migrate(source_db, target_db, relation, mapping_names,
            excluded, target_dir=tempdir, write=args.write,
            new_db=args.newdb, drop_fk=args.dropfk, del_csv=args.tmpfs,
            forget_missing=args.forgetmissing, owner=args.owner)
    print(u'The identifier for this migration is "{0}"'.format(identifier))

    if not args.keepcsv:
        shutil.rmtree(tempdir)


def migrate(source_db, target_db, source_tables, mapping_names,
            excluded=None, target_dir=None, write=False,
            new_db=False, drop_fk=False, del_csv=False,
            forget_missing=False, owner=False):
    """ The main migration function
    """
    start_time = time.time()
    source_connection = get_db_connection(dsn="dbname=%s" % source_db)
    if new_db:
        target_db = create_new_db(source_db, target_db, new_db, owner)
    target_connection = get_db_connection(dsn="dbname=%s" % target_db)

    # Get the list of modules installed in the target db
    with target_connection.cursor() as c:
        c.execute("select name from ir_module_module where state='installed'")
        target_modules = [m[0] for m in c.fetchall()]

    # we turn the list of wanted tables into the full list of required tables
    print(u'Computing the real list of tables to export...')
    if drop_fk:
        print(u'Normally you would get a list of dependencies here but we don\'t care'
              u' as we are dropping the constraints')
    source_tables, m2m_tables = add_related_tables(source_connection, source_tables,
                                                   excluded, show_log=not drop_fk)

    LOG.info(u'The real list of tables to export is:\n%s' % '\n'.join(
        make_a_nice_list(source_tables)))
    with open('export.txt', 'w') as f:
        f.write('\n'.join(make_a_nice_list(source_tables)))

    # construct the mapping and the csv processor
    print('Exporting tables as CSV files...')
    filepaths = export_to_csv(source_tables, target_dir, source_connection)
    for i, mapping_name in enumerate(mapping_names):
        if not exists(mapping_name):
            mapping_names[i] = join(HERE, 'mappings', mapping_name)
            LOG.warn('%s not found. Trying %s', mapping_name, mapping_names[i])
    mapping = Mapping(target_modules, mapping_names, drop_fk=drop_fk)
    processor = CSVProcessor(mapping)

    target_tables = processor.get_target_columns(
        filepaths, forget_missing, target_connection).keys()

    LOG.info(u'The real list of tables to import is:\n%s' % '\n'.join(
        make_a_nice_list(target_tables)))
    with open('import.txt', 'w') as f:
        f.write('\n'.join(make_a_nice_list(target_tables)))
    processor.mapping.set_database_ids(source_tables, source_connection,
                                       target_tables, target_connection)


    print('Computing the list of Foreign Keys '
          'to update in the target csv files...')
    processor.fk2update = get_fk_to_update(target_connection, target_tables)

    # update the list of fk to update with the fake __fk__ given in the mapping
    processor.fk2update.update(processor.mapping.fk2update)

    # extract the existing records from the target database
    existing_records = extract_existing(
        target_tables, m2m_tables, mapping.discriminators, target_connection)

    # create migrated csv files from exported csv files
    print(u'Migrating CSV files...')
    processor.set_existing_data(existing_records)
    processor.process(target_dir, filepaths, target_dir,
                      target_connection, del_csv=del_csv)
    # drop foreign key constraints
    if drop_fk:
        print(u'Dropping Foreign Key Constraints in target tables')
        target_connection.close()
        mgmt_connection = get_management_connection(source_db)
        with mgmt_connection.cursor() as m:
            kill_db_connections(m, target_db)
        mgmt_connection.close()
        add_constraints_sql = drop_constraints(db=target_db,
                                               tables=target_tables)
        if not add_constraints_sql:
            drop_fk = False
        else:
            with open('add_constraints.sql', 'w') as f:
                f.write(add_constraints_sql)

        target_connection = get_db_connection(dsn="dbname=%s" % target_db)

    # import data in the target
    print(u'Trying to import data in the target database...')
    target_files = [join(target_dir, '%s.target2.csv' % t) for
                    t in target_tables]
    remaining = import_from_csv(
        target_files, target_connection, drop_fk=drop_fk)
    if remaining:
        print(u'Please improve the mapping by inspecting the errors above')
        sys.exit(1)

    # execute deferred updates for preexisting data
    print(u'Updating pre-existing data...')
    filepaths = []
    for table in target_tables:
        filepath = join(target_dir, table + '.update2.csv')
        if exists(filepath):
            filepaths.append(filepath)
        else:
            LOG.warn(u'Not updating %s as it was not imported', table)
    processor.update_all(filepaths, target_connection, suffix="_temp")

    # Drop stored fields (e.g. related and computed)
    processor.drop_stored_columns(target_connection)

    if write:
        target_connection.commit()
        print(u'Finished, and transaction committed !! \o/')
    else:
        target_connection.rollback()
        print(u'Finished \o/ Use --write to really '
              u'write to the target database')
    target_connection.close()

    # Note we check here again just in case
    if all([(n.isalnum() or n == '_' for n in target_db)]):
        #First kill any connections
        s_mgmt_connection = get_management_connection(db=source_db)
        with s_mgmt_connection.cursor() as s:
            kill_db_connections(s, target_db)
            if new_db and not write:
                s.execute('DROP DATABASE IF EXISTS {0};'.format(target_db))
                print(u'Target Database dropped')
            elif drop_fk:
                print(u'Restoring Foreign Key Constraints')
                t_mgmt_connection = get_management_connection(db=target_db)
                with t_mgmt_connection.cursor() as t:
                    try:
                        t.execute(add_constraints_sql)
                    except Exception, e:
                        LOG.error('Error Restoring Constraints: A copy has '
                                  'been saved in add_constraints.sql\n'
                                  '%s' % e.message)
        print(u'Updating next database_ids')
        target_connection = get_db_connection(dsn="dbname=%s" % target_db)
        mapping.update_database_sequences(target_connection)

    seconds = time.time() - start_time
    lines = processor.lines
    rate = lines / seconds
    print(u'Migrated %s lines in %s seconds (%s lines/s)'
          % (processor.lines, int(seconds), int(rate)))


def make_a_nice_list(l):
    return sorted(l)