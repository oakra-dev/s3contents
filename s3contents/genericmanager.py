import asyncio
import datetime
import json
import mimetypes
import os

from dateutil.tz import tzutc
from fsspec.asyn import sync
from tornado.web import HTTPError

from s3contents.chunks import (
    assemble_chunks,
    delete_chunks,
    prune_stale_chunks,
    store_content_chunk,
)
from s3contents.genericfs import GenericFSError, NoSuchFile
from s3contents.ipycompat import (
    Any,
    ContentsManager,
    GenericFileCheckpoints,
    HasTraits,
    TraitError,
    Unicode,
    from_dict,
    import_item,
    reads,
    string_types,
    validate,
)

DUMMY_CREATED_DATE = datetime.datetime.fromtimestamp(86400)
NBFORMAT_VERSION = 4


class GenericContentsManager(ContentsManager, HasTraits):

    # This makes the checkpoints get saved on this directory
    root_dir = Unicode("./", config=True)

    post_save_hook = Any(
        None,
        config=True,
        allow_none=True,
        help="""Python callable or importstring thereof
        to be called on the path of a file just saved.
        This can be used to process the file on disk,
        such as converting the notebook to a script or HTML via nbconvert.
        It will be called as (all arguments passed by keyword)::
            hook(s3_path=s3_path, model=model, contents_manager=instance)
        - s3_path: the S3 path to the file just written (sans bucket/prefix)
        - model: the model representing the file
        - contents_manager: this ContentsManager instance
        """,
    )

    def __init__(self, *args, **kwargs):
        super(GenericContentsManager, self).__init__(*args, **kwargs)
        self._fs = None

    def get_fs(self):
        return self._fs

    fs = property(get_fs)

    def _checkpoints_class_default(self):
        return GenericFileCheckpoints

    def do_error(self, msg, code=500):
        raise HTTPError(code, msg)

    def no_such_entity(self, path):
        self.do_error("No such entity: [{path}]".format(path=path), 404)

    def already_exists(self, path):
        thing = "File" if self.file_exists(path) else "Directory"
        self.do_error(
            "{thing} already exists: [{path}]".format(thing=thing, path=path),
            409,
        )

    def guess_type(self, path, allow_directory=True):
        """
        Guess the type of a file.
        If allow_directory is False, don't consider the possibility that the
        file is a directory.

        Parameters
        ----------
            obj: s3.Object or string
        """
        self.log.debug(
            f"guess_type with path={path} and allow_directory={allow_directory}"
        )
        if path.endswith(".ipynb"):
            return "notebook"
        elif allow_directory and self.dir_exists(path):
            return "directory"
        else:
            return "file"

    def file_exists(self, path):
        # Does a file exist at the given path?
        self.log.debug("S3contents.GenericManager.file_exists: ('%s')", path)
        return self.fs.isfile(path)

    def dir_exists(self, path):
        # Does a directory exist at the given path?
        self.log.debug(
            "S3contents.GenericManager.dir_exists: path('%s')", path
        )
        return self.fs.isdir(path)

    def get(self, path, content=True, type=None, format=None):
        # Get a file or directory model.
        self.log.debug(
            "S3contents.GenericManager.get: path('%s') type(%s) format(%s)",
            path,
            type,
            format,
        )

        # This is a hack to remove some startup dialog error from JupyterLab
        # TODO: Figure out why is this happening
        if self.parent:
            if path.startswith(self.parent.root_dir):
                self.log.debug(
                    "S3contents.GenericManager.get: removing root_dir (%s) from path",
                    self.parent.root_dir,
                )
                path = path[len(self.parent.root_dir) :]
        # END hack

        path = path.strip("/")

        if type is None:
            type = self.guess_type(path)
        try:
            func = {
                "directory": self._get_directory,
                "notebook": self._get_notebook,
                "file": self._get_file,
            }[type]
        except KeyError:
            raise ValueError("Unknown type passed: '{}'".format(type))

        return func(path=path, content=content, format=format)

    def _get_directory(self, path, content=True, format=None):
        self.log.debug(
            "S3contents.GenericManager._get_directory: path('%s') content(%s) format(%s)",
            path,
            content,
            format,
        )
        return self._directory_model_from_path(path, content=content)

    def _get_notebook(self, path, content=True, format=None):
        self.log.debug(
            "S3contents.GenericManager._get_notebook: path('%s') type(%s) format(%s)",
            path,
            content,
            format,
        )
        return self._notebook_model_from_path(
            path, content=content, format=format
        )

    def _get_file(self, path, content=True, format=None):
        self.log.debug(
            "S3contents.GenericManager._get_file: path('%s') type(%s) format(%s)",
            path,
            content,
            format,
        )
        return self._file_model_from_path(path, content=content, format=format)

    def _directory_model_from_path(self, path, content=False):
        self.log.debug(
            "S3contents.GenericManager._directory_model_from_path: path('%s') type(%s)",
            path,
            content,
        )
        model = base_directory_model(path)
        if self.fs.isdir(path):
            lstat = self.fs.lstat(path)
            if "ST_MTIME" in lstat and lstat["ST_MTIME"]:
                model["created"] = model["last_modified"] = lstat["ST_MTIME"]

            self.log.debug(
                f"s3_detail_to_model={self.convert_s3_details_to_models}"
                f"dir_s3_detail: path='{path}', lstat={lstat}"
            )
        if content:
            if not self.dir_exists(path):
                self.no_such_entity(path)
            model["format"] = "json"
            prefixed_path = self.fs.path(path)
            files_s3_detail = sync(self.fs.fs.loop, self.fs.fs._lsdir, prefixed_path)
            filtered_files_s3_detail = list(
                filter(
                    lambda detail: os.path.basename(detail["Key"]) != self.fs.dir_keep_file and self.fs.remove_prefix(detail["Key"]) != path,
                    files_s3_detail,
                )
            )
            filtered_files_s3_detail = sync(self.fs.fs.loop, self.get_content_s3_metadata, filtered_files_s3_detail)
            filtered_files_s3_detail = self.filter_deleted_markers(filtered_files_s3_detail)
            model['content'] = self.convert_s3_details_to_models(filtered_files_s3_detail)
        return model

    def filter_deleted_markers(self, s3_details):
        def is_delete_marker(detail):
            return bool("ST_MTIME" in detail and detail["ST_MTIME"])
        return list(filter(is_delete_marker, s3_details))

    async def get_content_s3_metadata(self, s3_details):
        async def s3_detail_metadata(s3_detail):
            if s3_detail["StorageClass"] == "DIRECTORY":
                dir_path = os.path.join(self.fs.path(s3_detail["Key"]), ".s3keep")
                try:
                    lstat = await self.fs.fs._info(dir_path)
                    s3_detail['LastModified'] = lstat['LastModified']
                except FileNotFoundError:
                    pass
            st_time = s3_detail.get("LastModified")
            if st_time:
                s3_detail["ST_MTIME"] = datetime.datetime(
                    st_time.year,
                    st_time.month,
                    st_time.day,
                    st_time.hour,
                    st_time.minute,
                    st_time.second,
                    tzinfo=st_time.tzinfo,
                )
            return s3_detail

        tasks = [self.fs.fs.loop.create_task(s3_detail_metadata(detail)) for detail in s3_details]
        details_with_meta = await asyncio.gather(*tasks, loop=self.fs.fs.loop)
        return details_with_meta

    def convert_s3_details_to_models(self, s3_details):
        models = []
        for s3_detail in s3_details:
            model_path = s3_detail["Key"]
            model = base_model(self.fs.remove_prefix(model_path))
            if s3_detail["StorageClass"] == "DIRECTORY":
                model["created"] = model["last_modified"] = DUMMY_CREATED_DATE
                model["type"] = "directory"
                model['last_modified'] = model['created'] = s3_detail.get('LastModified')
                if "ST_MTIME" in s3_detail and s3_detail["ST_MTIME"]:
                    model["created"] = model["last_modified"] = s3_detail["ST_MTIME"]
            else:
                model["last_modified"] = s3_detail.get("LastModified").replace(microsecond=0, tzinfo=tzutc())
                model["created"] = model["last_modified"]
                model["type"] = (
                    "notebook" if model_path.endswith(".ipynb") else "file"
                )
            models.append(model)
        return models

    def _notebook_model_from_path(self, path, content=False, format=None):
        """
        Build a notebook model from database record.
        """
        self.log.debug(
            f"_notebook_model_from_path with path={path}, content={content}, format={format}"
        )
        model = base_model(path)
        model["type"] = "notebook"
        if self.fs.isfile(path):
            model["created"] = model["last_modified"] = self.fs.lstat(path)[
                "ST_MTIME"
            ]
        else:
            self.do_error("Not Found", 404)
        if content:
            if not self.fs.isfile(path):
                self.no_such_entity(path)
            file_content, _ = self.fs.read(path, format)
            nb_content = reads(file_content, as_version=NBFORMAT_VERSION)
            self.mark_trusted_cells(nb_content, path)
            model["format"] = "json"
            model["content"] = nb_content
            self.validate_notebook_model(model)
        return model

    def _file_model_from_path(self, path, content=False, format=None):
        """
        Build a file model from database record.
        """
        self.log.debug(
            f"_file_model_from_path with path={path}, content={content}, format={format}"
        )
        model = base_model(path)
        model["type"] = "file"
        if self.fs.isfile(path):
            model["created"] = model["last_modified"] = self.fs.lstat(path)[
                "ST_MTIME"
            ]
        else:
            model["created"] = model["last_modified"] = DUMMY_CREATED_DATE
        if content:
            try:
                # Get updated format from fs.read()
                content, format_ = self.fs.read(path, format)
            except NoSuchFile as e:
                self.no_such_entity(e.path)
            except GenericFSError as e:
                self.do_error(str(e), 500)
            model["format"] = format_
            model["content"] = content
            model["mimetype"] = mimetypes.guess_type(path)[0] or "text/plain"
        return model

    def save(self, model, path):
        """Save a file or directory model to path."""

        self.log.debug(f"save with path={path}, model={model}")

        # Chunked uploads
        # See https://jupyter-notebook.readthedocs.io/en/stable/extending/contents.html#chunked-saving
        chunk = model.get("chunk", None)
        if chunk is not None:
            return self._save_large_file(
                chunk, model, path, model.get("format")
            )

        self.log.debug("S3contents.GenericManager.save %s: '%s'", model, path)
        if "type" not in model:
            self.do_error("No model type provided", 400)
        if "content" not in model and model["type"] != "directory":
            self.do_error("No file content provided", 400)

        if model["type"] not in ("file", "directory", "notebook"):
            self.do_error("Unhandled contents type: %s" % model["type"], 400)

        self.run_pre_save_hook(model=model, path=path)

        try:
            if model["type"] == "notebook":
                validation_message = self._save_notebook(model, path)
            elif model["type"] == "file":
                validation_message = self._save_file(model, path)
            else:
                validation_message = self._save_directory(path)
        except Exception as e:
            self.log.error(
                "Error while saving file: %s %s", path, e, exc_info=True
            )
            self.do_error(
                "Unexpected error while saving file: %s %s" % (path, e), 500
            )

        model = self.get(path, type=model["type"], content=False)

        self.run_post_save_hook(model=model, s3_path=model["path"])

        if validation_message is not None:
            model["message"] = validation_message
        return model

    def _save_large_file(self, chunk, model, path, format):
        if "type" not in model:
            self.do_error("No file type provided", 400)
        if model["type"] != "file":
            self.do_error(
                'File type "{}" is not supported for large file transfer'.format(
                    model["type"]
                ),
                400,
            )
        if "content" not in model and model["type"] != "directory":
            self.do_error("No file content provided", 400)

        if format not in {"text", "base64"}:
            self.do_error(
                "Must specify format of file contents as 'text' or 'base64'",
                400,
            )

        prune_stale_chunks()

        self.log.debug(
            "S3contents.GenericManager.save (chunk %s) %s: '%s'",
            chunk,
            model,
            path,
        )

        try:
            if chunk == 1:
                self.run_pre_save_hook(model=model, path=path)
            # Store the chunk in our registry
            store_content_chunk(path, model["content"])
        except Exception as e:
            self.log.error(
                "S3contents.GenericManager._save_large_file: error while saving file: %s %s",
                path,
                e,
                exc_info=True,
            )
            self.do_error(f"Unexpected error while saving file: {path} {e}")

        if chunk == -1:
            # Last chunk: we want to combine the chunks in the registry to compose the full file content
            model["content"] = assemble_chunks(path)
            delete_chunks(path)
            self._save_file(model, path)

        return self.get(path, content=False)

    def _save_notebook(self, model, path):
        nb_contents = from_dict(model["content"])
        self.check_and_sign(nb_contents, path)
        file_contents = json.dumps(model["content"])
        file_format = model.get("format")
        self.fs.write(path, file_contents, file_format)
        self.validate_notebook_model(model)
        return model.get("message")

    def _save_file(self, model, path):
        file_contents = model["content"]
        file_format = model.get("format")
        self.fs.write(path, file_contents, file_format)

    def _save_directory(self, path):
        self.fs.mkdir(path)

    def rename_file(self, old_path, new_path):
        """Rename a file or directory.

        NOTE: This method is unfortunately named on the base class.  It
        actually moves a file or a directory.
        """
        self.log.debug(
            "S3contents.GenericManager.rename_file: Init rename of '%s' to '%s'",
            old_path,
            new_path,
        )
        if self.file_exists(new_path) or self.dir_exists(new_path):
            self.already_exists(new_path)
        elif self.file_exists(old_path) or self.dir_exists(old_path):
            self.log.debug(
                "S3contents.GenericManager: Actually renaming '%s' to '%s'",
                old_path,
                new_path,
            )
            self.fs.mv(old_path, new_path)
        else:
            self.no_such_entity(old_path)

    def delete_file(self, path):
        """Delete the file or directory at path."""
        self.log.debug("S3contents.GenericManager.delete_file '%s'", path)
        if self.file_exists(path) or self.dir_exists(path):
            self.fs.rm(path)
        else:
            self.no_such_entity(path)

    def is_hidden(self, path):
        """Is path a hidden directory or file?"""
        self.log.debug("S3contents.GenericManager.is_hidden '%s'", path)
        return False

    @validate("post_save_hook")
    def _validate_post_save_hook(self, proposal):
        value = proposal["value"]
        if isinstance(value, string_types):
            value = import_item(value)
        if not callable(value):
            raise TraitError("post_save_hook must be callable")
        return value

    def run_post_save_hook(self, model, s3_path):
        """Run the post-save hook if defined, and log errors"""
        if self.post_save_hook:
            try:
                self.log.debug("Running post-save hook on %s", s3_path)
                self.post_save_hook(
                    s3_path=s3_path, model=model, contents_manager=self
                )
            except Exception as e:
                self.log.error(
                    "Post-save hook failed o-n %s", s3_path, exc_info=True
                )
                raise HTTPError(
                    500,
                    "Unexpected error while running post hook save: %s" % e,
                ) from e


def base_model(path):
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "writable": True,
        "last_modified": None,
        "created": None,
        "content": None,
        "format": None,
        "mimetype": None,
    }


def base_directory_model(path):
    model = base_model(path)
    model.update(
        type="directory",
        last_modified=DUMMY_CREATED_DATE,
        created=DUMMY_CREATED_DATE,
    )
    return model
