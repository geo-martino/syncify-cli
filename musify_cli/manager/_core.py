from __future__ import annotations

import json
import logging
import logging.config
from collections.abc import Collection
from datetime import datetime
from os.path import splitext
from time import perf_counter
from typing import Any

import yaml
from jsonargparse import Namespace
from musify import MODULE_ROOT as MUSIFY_ROOT
from musify.libraries.core.collection import MusifyCollection
from musify.libraries.remote.core.enum import RemoteObjectType
from musify.libraries.remote.core.object import SyncResultRemotePlaylist
from musify.log import STAT
from musify.log.logger import MusifyLogger
from musify.processors.download import ItemDownloadHelper
from musify.report import report_playlist_differences, report_missing_tags
from musify.types import UnitIterable

from musify_cli import MODULE_ROOT
from musify_cli.exception import ParserError
from musify_cli.manager.library import LocalLibraryManager, MusicBeeManager
from musify_cli.manager.library import RemoteLibraryManager, SpotifyLibraryManager
from musify_cli.parser import LoadTypesRemote, EnrichTypesRemote


class ReportsManager:
    """Configures options for running reports on Musify objects from a given ``config``."""
    def __init__(self, config: Namespace, parent: MusifyManager):
        self.config = config
        self.parent: MusifyManager = parent

    def __call__(self) -> None:
        self.playlist_differences()
        self.missing_tags()

    def playlist_differences(self) -> None:
        """Generate a report on the differences between two library's playlists."""
        config = self.config.library_differences
        if not config.enabled:
            return

        report_playlist_differences(
            source=config.filter(self.parent.local.library.playlists.values()),
            reference=config.filter(self.parent.remote.library.playlists.values())
        )

    def missing_tags(self) -> None:
        """Generate a report on the items in albums from the local library that have missing tags."""
        config = self.config.missing_tags
        if not config.enabled:
            return

        source = config.filter(self.parent.local.library.albums)
        report_missing_tags(collections=source, tags=config.tags, match_all=config.match_all)


class MusifyManager:
    """General class for managing various Musify objects, configured from a given ``config``."""

    _local_library_map: dict[str, type[LocalLibraryManager]] = {
        "local": LocalLibraryManager,
        "musicbee": MusicBeeManager,
    }

    _remote_library_map: dict[str, type[RemoteLibraryManager]] = {
        "spotify": SpotifyLibraryManager,
    }

    def __init__(self, config: Namespace):
        start_time = perf_counter()

        # noinspection PyTypeChecker
        self.logger: MusifyLogger = logging.getLogger(__name__)

        self.config = config
        self.dt = datetime.now()

        self._output_folder: str | None = None
        self._dry_run: bool | None = None

        local_library_config: Namespace = self.config.libraries.local
        self.local: LocalLibraryManager = self._local_library_map[local_library_config.type](
            name=local_library_config.name,
            config=local_library_config.get(local_library_config.type),
            dry_run=self.dry_run,
        )

        remote_library_config: Namespace = self.config.libraries.remote
        self.remote: RemoteLibraryManager = self._remote_library_map[remote_library_config.type](
            name=remote_library_config.name,
            config=remote_library_config.get(remote_library_config.type),
            dry_run=self.dry_run,
        )

        self.local._remote_wrangler = self.remote.wrangler

        self.reports: ReportsManager = ReportsManager(config=self.config.reports, parent=self)

        setup_time = perf_counter() - start_time
        self.logger.debug(f"{self.__class__.__name__} initialised. Time taken: {setup_time:.3f}")

    @property
    def output_folder(self) -> str:
        """Directory of the folder to use for output data"""
        if self._output_folder is None:
            self._output_folder = self.config.output
        return self._output_folder

    @property
    def dry_run(self) -> bool:
        """Whether to run all write operations"""
        if self._dry_run is None:
            self._dry_run = not self.config.execute
        return self._dry_run

    @property
    def backup_key(self) -> str | None:
        """The key to give to backups + the key to restore from"""
        return self.config.backup.key

    ###########################################################################
    ## Setup
    ###########################################################################
    @classmethod
    def configure_logging(cls, path: str, name: str | None = None, *names: str) -> None:
        """
        Load logging config from a configured JSON or YAML file using logging.config.dictConfig.

        :param path: The path to the logger config
        :param name: If the given name is a valid logger name in the config,
            assign this logger's config to the module root logger.
        :param names: When given, also apply the config from ``name`` to loggers with these ``names``.
        """
        ext = splitext(path)[1].casefold()

        allowed = {".yml", ".yaml", ".json"}
        if ext not in allowed:
            raise ParserError(
                "Unrecognised log config file type: {key}. Valid: {value}", key=ext, value=allowed
            )

        with open(path, "r", encoding="utf-8") as file:
            if ext in {".yml", ".yaml"}:
                log_config = yaml.full_load(file)
            elif ext in {".json"}:
                log_config = json.load(file)

        MusifyLogger.compact = log_config.pop("compact", False)
        MusifyLogger.disable_bars = log_config.pop("disable_bars", True)

        for formatter in log_config["formatters"].values():  # ensure ANSI colour codes in format are recognised
            formatter["format"] = formatter["format"].replace(r"\33", "\33")

        if name and name in log_config.get("loggers", {}):
            log_config["loggers"][MODULE_ROOT] = log_config["loggers"][name]
            log_config["loggers"][MUSIFY_ROOT] = log_config["loggers"][name]
            for n in names:
                log_config["loggers"][n] = log_config["loggers"][name]

        logging.config.dictConfig(log_config)

        if name and name in log_config.get("loggers", {}):
            logging.getLogger(MODULE_ROOT).debug(f"Logging config set to: {name}")

    ###########################################################################
    ## Pre-/Post- operations
    ###########################################################################
    def run_pre(self) -> None:
        """Run all pre-processor operations."""
        self.load(True)

    def run_post(self) -> None:
        """Run all post-processor operations."""
        self.pause()

    def load(self, force: bool = False) -> None:
        """Reload the libraries according to the configured settings."""
        config_local = self.config.reload.local
        if config_local.types:
            self.logger.debug("Load local library: START")
            self.local.load(types=config_local.types or (), force=force)
            self.logger.debug("Load local library: DONE")

        config_remote = self.config.reload.remote
        if any([config_remote.types, config_remote.extend, config_remote.enrich.enabled]):
            self.logger.debug("Load remote library: START")
            self.remote.load(
                types=config_remote.types or (),
                extend=self.local.library if config_remote.extend else (),
                enrich=config_remote.enrich.enabled or False,
                enrich_types=config_remote.enrich.types or (),
                force=force
            )
            self.logger.debug("Load remote library: DONE")

    def pause(self) -> None:
        """Pause the application and display message if configured."""
        if self.config.pause:
            input(f"\33[93m{self.config.pause}\33[0m ")
            self.logger.print()

    ###########################################################################
    ## Utilities
    ###########################################################################
    def filter[T: Any](self, items: Collection[T]) -> Collection[T]:
        """Run the generic filter on the given ``items`` if configured."""
        if self.config.filter.ready:
            return self.config.filter(items)
        return items

    ###########################################################################
    ## Operations
    ###########################################################################
    def run_download_helper(self, collections: UnitIterable[MusifyCollection]) -> None:
        """Run the :py:class:`ItemDownloadHelper` for the given ``collections``"""
        download_helper = ItemDownloadHelper(
            urls=self.config.download.urls,
            fields=self.config.download.fields,
            interval=self.config.download.interval,
        )
        download_helper(collections)

    def create_new_music_playlist(self) -> tuple[str, SyncResultRemotePlaylist]:
        """
        Create a new music playlist for followed artists with music released between ``start`` and ``end``.

        :return: The name of the new playlist and results of the sync as a :py:class:`SyncResultRemotePlaylist` object.
        """
        name = self.config.new_music.name
        start = self.config.new_music.start
        end = self.config.new_music.end

        self.logger.info(
            f"\33[1;95m ->\33[1;97m Creating '{name}' {self.remote.source} playlist "
            f"for new tracks by followed artists released between {start} and {end} \33[0m"
        )

        # load saved artists and their albums with fresh data, ignoring use_cache settings
        load_albums = any([
            LoadTypesRemote.saved_artists not in self.remote.types_loaded,
            EnrichTypesRemote.albums not in self.remote.types_enriched[LoadTypesRemote.saved_artists]
        ])
        if load_albums:
            self.remote.library.use_cache = False
            self.remote.load(types=[LoadTypesRemote.saved_artists])
            self.remote.library.use_cache = self.remote.use_cache
            self.remote.library.enrich_saved_artists(types=("album", "single"))

        # filter and extend loaded albums
        albums = self.remote.filter_artist_albums_by_date(start=start, end=end)
        albums_need_extend = [album for album in albums if len(album.tracks) < album.track_total]
        if albums_need_extend:
            kind = RemoteObjectType.ALBUM
            key = self.remote.api.collection_item_map[kind]

            bar = self.logger.get_progress_bar(iterable=albums_need_extend, desc="Getting album tracks", unit="albums")
            for album in bar:
                self.remote.api.extend_items(album.response, kind=kind, key=key, use_cache=self.remote.use_cache)
                album.refresh(skip_checks=False)

        # log load results
        if load_albums or albums_need_extend:
            self.logger.print(STAT)
            self.remote.library.log_artists()
            self.logger.print()

        tracks = [track for album in sorted(albums, key=lambda x: x.date, reverse=True) for track in album]
        self.logger.info(f"\33[1;95m  >\33[1;97m Found {len(tracks)} tracks to add to {name!r} \33[0m")

        # add tracks to remote playlist
        pl = self.remote.get_or_create_playlist(name)
        pl.clear()
        pl.extend(tracks, allow_duplicates=False)
        return name, pl.sync(kind="refresh", reload=False, dry_run=self.config.dry_run)
