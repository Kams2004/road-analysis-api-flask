"""API schemas of cloud sources: browsing a drive and import batches."""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

ItemStatus = Literal["queued", "downloading", "uploading_to_storage", "queued_for_processing", "processing",
                     "done", "failed", "skipped_duplicate", "cancelled"]
BatchStatus = Literal["queued", "running", "done", "completed_with_errors", "failed", "cancelled"]


class ProviderOut(BaseModel):
    """A drive provider the platform can import from."""
    id:                  str = Field(description="Provider id used as `provider`.", examples=["google_drive"])
    label:               str = Field(description="Display name.", examples=["Google Drive"])
    auth:                Literal["oauth", "server"] = Field(
        "oauth", description="`oauth`: an account consents at the provider (`POST /cloud-connections/`); "
                             "`server`: an SFTP server profile whose password is set in the secrets file "
                             "(`POST /cloud-connections/sftp`).")
    configured:          bool = Field(description="Its OAuth client is set in *Platform settings → cloud* (always "
                                                  "`true` for SFTP); connections can only be created when `true`.")
    supports_search:     bool = Field(description="`q` is supported by `/browse`.")
    search_recursive:    bool = Field(description="Search also covers sub-folders (OneDrive).")
    supports_thumbnails: bool = Field(description="Files may have a thumbnail (`has_thumbnail`).")
    supports_resume:     bool = Field(description="Interrupted downloads resume where they stopped.")


class CrumbOut(BaseModel):
    id:   str = Field(description="Folder id.")
    name: str = Field(description="Folder name.", examples=["Dashcam"])


class ImportInfoOut(BaseModel):
    """Whether this file (in its current version) was imported before."""
    state:    Literal["none", "importing", "imported", "changed"] = Field(
        description="`importing` (an import or its job is in progress), `imported` (this version is done), "
                    "`changed` (an older version was imported), `none`.")
    job_id:   Optional[str] = Field(None, description="Job of the latest import of the file.")
    item_id:  Optional[str] = Field(None, description="Import item of the latest import.")
    batch_id: Optional[str] = Field(None, description="Import batch of the latest import.")


class DriveEntryOut(BaseModel):
    """A folder or file of the drive (SFTP: of the server)."""
    id:            str = Field(description="Provider id (folder: use as `folder_id`). SFTP: the absolute remote path.")
    name:          str = Field(description="Name.", examples=["2026-10-14_dashcam.mp4"])
    kind:          Literal["folder", "file"]
    mime_type:     Optional[str] = Field(None, examples=["video/mp4"])
    media:         Optional[Literal["video", "image"]] = Field(
        None, description="Media type the processor accepts; `null` = not importable (shown disabled).")
    importable:    bool = Field(description="Can be imported (supported video or image).")
    size:          Optional[int] = Field(None, description="Bytes (`null` when the provider does not tell).")
    modified:      Optional[str] = Field(None, description="Last modification (provider timestamp, ISO 8601).")
    duration_s:    Optional[float] = Field(None, description="Video length when the provider gives it.")
    width:         Optional[int] = None
    height:        Optional[int] = None
    child_count:   Optional[int] = Field(None, description="Items in the folder (OneDrive only).")
    has_thumbnail: bool = Field(False, description="`GET /cloud-connections/{id}/thumbnail/{file_id}` returns an image.")
    web_url:       Optional[str] = Field(None, description="Open in the provider's web UI.")
    path:          Optional[str] = Field(None, description="Folder path (search results that include sub-folders).")
    imported:      Optional[ImportInfoOut] = Field(None, description="Files only: previous imports.")


class BrowseOut(BaseModel):
    """One page of a drive folder."""
    connection_id:   str
    folder_id:       Optional[str] = Field(description="Folder listed (`null` = drive root).")
    breadcrumbs:     list[CrumbOut] = Field(description="From the drive root (excluded) down to this folder.")
    folders:         list[DriveEntryOut]
    files:           list[DriveEntryOut]
    next_page_token: Optional[str] = Field(None, description="Pass as `page_token` for the next page.")
    query:           Optional[str] = Field(None, description="Search applied.")
    search_recursive: bool = Field(False, description="The search covered sub-folders too.")


class ImportOptions(BaseModel):
    include_subfolders: bool = Field(True, description="Folders in `folder_ids` are imported with every "
                                                       "sub-folder (else only their own files).")
    reimport:           bool = Field(False, description="Import again files whose current version was already "
                                                        "imported (else they are `skipped_duplicate`).")


class ImportIn(BaseModel):
    """Files and/or folders to import from the drive."""
    files:      list[str] = Field(default_factory=list, max_length=1000,
                                  description="Provider file ids.", examples=[["1AbC…", "1DeF…"]])
    folder_ids: list[str] = Field(default_factory=list, max_length=50,
                                  description="Folders whose media files are imported (see `include_subfolders`).")
    models:     Optional[list[str]] = Field(None, description="Models to run (`pothole`, `signs`, `speedbump`, "
                                                              "`vehicles`); `null` = all but `vehicles`.",
                                            examples=[["pothole", "signs", "speedbump"]])
    frame_skip: Optional[int] = Field(None, ge=1, le=60, description="Process one frame out of N (default: "
                                                                     "processing.frame_skip_by_source).")
    options:    ImportOptions = Field(default_factory=ImportOptions)


class ImportItemOut(BaseModel):
    """One file of an import batch, with its live progress."""
    id:            str
    batch_id:      str
    file_id:       str = Field(description="Provider file id.")
    name:          str
    path:          Optional[str] = Field(None, description="Folder path inside the imported folder.")
    mime_type:     Optional[str] = None
    status:        ItemStatus = Field(description=(
        "`queued` → `downloading` → `uploading_to_storage` (verify + store) → `queued_for_processing` → "
        "`processing` → `done`; or `failed`, `cancelled`, `skipped_duplicate`. After hand-over the status "
        "follows the job."))
    phase_status:  str = Field(description="Status of the import step itself (before the job takes over).")
    bytes_total:   Optional[int] = Field(None, description="File size (`null` = unknown → indeterminate bar).")
    bytes_done:    int = Field(0, description="Bytes downloaded.")
    percent:       Optional[float] = Field(None, description="Download progress 0–100 (`null` when the size is unknown).")
    speed_bps:     Optional[float] = Field(None, description="Download speed, bytes/s (moving average).")
    eta_s:         Optional[float] = Field(None, description="Estimated seconds left for the download.")
    attempts:      int = Field(0, description="Download attempts.")
    error:         Optional[str] = Field(None, description="Readable failure reason (download or processing).")
    job_id:        Optional[str] = Field(None, description="Processing job once handed over.")
    job_status:    Optional[str] = None
    job_progress:  Optional[float] = Field(None, description="Processing progress 0–1 (from the job).")
    job_detections: Optional[int] = None
    duplicate_of_job_id: Optional[str] = Field(None, description="`skipped_duplicate`: the job of the earlier import.")
    created_at:    datetime
    started_at:    Optional[datetime] = None
    finished_at:   Optional[datetime] = None
    updated_at:    Optional[datetime] = None


class ImportBatchOut(BaseModel):
    """An import: the files picked in one go (or found by one watched-folder run)."""
    id:              str
    connection_id:   Optional[str] = None
    provider:        str
    connection_name: Optional[str] = None
    origin:          Literal["manual", "sync"] = Field(description="`sync` = watched-folder automatic import.")
    models:          list[str]
    frame_skip:      Optional[int] = None
    options:         dict = Field(default_factory=dict)
    item_count:      int
    cancel_requested: bool
    created_by:      Optional[str] = None
    created_at:      datetime
    finished_at:     Optional[datetime] = None
    status:          BatchStatus
    counts:          dict[str, int] = Field(description="Items per status.")
    bytes_total:     int = Field(description="Bytes to download (known sizes).")
    bytes_done:      int
    speed_bps:       Optional[float] = Field(None, description="Combined download speed.")
    download_eta_s:  Optional[float] = Field(None, description="Seconds left for the downloads at the current speed.")
    progress:        float = Field(description="Overall 0–100: download = first half, processing = second half "
                                               "of each file.")
    active:          bool = Field(description="Something is still in progress (poll again).")
    items:           Optional[list[ImportItemOut]] = Field(None, description="Present on the detail endpoint and "
                                                                             "with `include_items=true`.")


class ImportBatchListOut(BaseModel):
    total:  int
    active: int = Field(description="Batches still in progress.")
    items:  list[ImportBatchOut]
