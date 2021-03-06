import csv
import os
import time
from collections import OrderedDict

import rows
from django.core.cache import cache
from django.db import transaction
from django.db.utils import ProgrammingError
from django.utils import timezone

from core.models import DataTable, Field, Table


class ImportDataCommand:
    def __init__(self, table, **options):
        self.table = table
        self.flag_import_data = options["import_data"]
        self.flag_vacuum = options["vacuum"]
        self.flag_clear_view_cache = options["clear_view_cache"]
        self.flag_create_filter_indexes = options["create_filter_indexes"]
        self.flag_fill_choices = options["fill_choices"]
        self.flag_delete_old_table = options["delete_old_table"]
        self.collect_date = options["collect_date"]

    def log(self, msg, *args, **kwargs):
        print(msg, *args, **kwargs)

    @classmethod
    def execute(cls, dataset_slug, tablename, filename, **options):
        table = Table.with_hidden.for_dataset(dataset_slug).named(tablename)
        self = cls(table, **options)
        data_table = DataTable.new_data_table(table)  # in memory instance, not persisted in the DB

        if self.flag_import_data:
            self.log(f"Importing data to new table {data_table.db_table_name}")
            Model = self.table.get_model(cache=False, data_table=data_table)
            self.import_data(filename, Model)
        else:
            Model = self.table.get_model(cache=False)

        # Vaccum and concurrent index creation cannot run inside a transaction block
        if self.flag_vacuum:
            self.run_vacuum(Model)
        if self.flag_create_filter_indexes:
            self.create_filter_indexes(Model)

        try:
            with transaction.atomic():
                if self.flag_fill_choices:
                    self.fill_choices(Model)
                if self.flag_import_data:
                    table.data_table.deactivate(drop_table=self.flag_delete_old_table)
                    data_table.activate()
        except Exception as e:
            self.log(f"Deleting import table {data_table.db_table_name} due to an error.")
            data_table.delete_data_table()
            raise e

        if self.flag_clear_view_cache:
            self.log("Clearing view cache...")
            cache.clear()

    def import_data(self, filename, Model):
        # Create the table if not exists
        with transaction.atomic():
            try:
                Model.delete_table()
            except ProgrammingError:  # Does not exist
                pass
            finally:
                Model.create_table(create_indexes=False)
                Model.create_triggers()

        # Get file object, header and set command to run
        table_name = Model._meta.db_table
        database_uri = os.environ["DATABASE_URL"]
        encoding = "utf-8"  # TODO: receive as a parameter
        timeout = 0.1  # TODO: receive as a parameter
        start_time = time.time()
        progress = rows.utils.ProgressBar(prefix="Importing data", unit="bytes")

        sample_size = 1 * 1024 * 1024  # 1 MiB
        with rows.utils.open_compressed(filename, mode="rb") as fobj:
            sample = fobj.read(sample_size)
            dialect = rows.plugins.csv.discover_dialect(sample, encoding)
        with rows.utils.open_compressed(filename) as fobj:
            reader = csv.DictReader(fobj, dialect=dialect)
            file_header = reader.fieldnames
        table_schema = self.table.schema
        schema = OrderedDict([(field_name, table_schema[field_name]) for field_name in file_header])
        try:
            import_meta = rows.utils.pgimport(
                filename=filename,
                encoding=encoding,
                dialect=dialect,
                database_uri=database_uri,
                table_name=table_name,
                create_table=False,
                timeout=timeout,
                callback=progress.update,
                schema=schema,
            )
        except RuntimeError as exception:
            progress.close()
            Model.delete_table()
            self.log("ERROR: {}".format(exception.args[0]))
            exit(1)
        else:
            progress.close()
            self.table.import_date = timezone.now()
            self.table.save()
            if self.collect_date:
                self.table.version.collected_at = self.collect_date
                self.table.version.save()
            end_time = time.time()
            duration = end_time - start_time
            rows_imported = import_meta["rows_imported"]
            self.log(
                "  done in {:7.3f}s ({} rows imported, {:.3f} rows/s).".format(
                    duration, rows_imported, rows_imported / duration
                )
            )
        self.table.invalidate_cache()

    def run_vacuum(self, Model):
        self.log("Running VACUUM ANALYSE...", end="", flush=True)
        start = time.time()
        Model.analyse_table()
        end = time.time()
        self.log("  done in {:.3f}s.".format(end - start))

    def create_filter_indexes(self, Model):
        # TODO: warn if field has_choices but not in Table.filtering
        self.log("Creating filter indexes...", end="", flush=True)
        start = time.time()
        Model.create_indexes()  # TODO: add "IF NOT EXISTS"
        end = time.time()
        self.log("  done in {:.3f}s.".format(end - start))

    def fill_choices(self, Model):
        self.log("Filling choices...")
        start = time.time()
        choiceables = Field.objects.for_table(self.table).choiceables()
        for field in choiceables:
            self.log("  {}".format(field.name), end="", flush=True)
            start_field = time.time()
            field.update_choices()
            field.save()
            end_field = time.time()
            self.log(" - done in {:.3f}s.".format(end_field - start_field))
        end = time.time()
        self.log("  done in {:.3f}s.".format(end - start))
