# -*- coding: utf-8 -*-
#
# Better Lyrics Plugin for MusicBrainz Picard 3.0+ & 2.x
#

from .better_lyrics import (
    BetterLyricsGetAction,
    BetterLyricsOptionsPage,
    BetterLyricsSearchAction,
    get_on_load,
    get_on_save,
)


def enable(api) -> None:
    """Entry point called when the plugin is enabled in MusicBrainz Picard 3.0+."""
    # In Plugin v3, file processors receive `api` as their first argument
    def v3_get_on_load(_api, track, file):
        get_on_load(track, file)

    def v3_get_on_save(_api, file):
        get_on_save(file)

    api.register_file_post_addition_to_track_processor(v3_get_on_load)
    api.register_file_post_save_processor(v3_get_on_save)

    api.register_track_action(BetterLyricsSearchAction)
    api.register_album_action(BetterLyricsSearchAction)
    api.register_track_action(BetterLyricsGetAction)
    api.register_album_action(BetterLyricsGetAction)

    api.register_options_page(BetterLyricsOptionsPage)


def disable() -> None:
    """Cleanup hook called when the plugin is disabled in Picard 3.0+."""
    pass
