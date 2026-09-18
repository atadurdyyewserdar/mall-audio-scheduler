from __future__ import annotations

import random
import time
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioBuffer, QAudioBufferOutput, QAudioDevice, QAudioFormat, QAudioOutput, QMediaPlayer


# A scheduled moment queues the entire voice-ad list, and two rules that land
# together queue it twice; the bound only guards against an unattended machine
# piling up work it can never get through.
MAX_QUEUED_ANNOUNCEMENTS = 64

SPECTRUM_BANDS = 48
# The decoder hands over ~1152 frames at a time, so analysing one buffer gave an
# FFT whose size - and so whose frequency resolution - changed with every short
# buffer. A fixed rolling window fixes the resolution and discards no audio.
#
# 1024 was measured, not assumed: against a click track of known tempo it
# correlated best with the real bass envelope (0.72 vs 0.65 at 4096), followed
# transients with no measurable lag (vs 32 ms), and kept the most peak-to-mean
# contrast. A longer window buys frequency resolution the display cannot show
# and pays for it by smearing every transient.
SPECTRUM_WINDOW = 1024
# Measured across real material, band levels here span roughly -80..+40 dBFS.
# The window is deliberately wider than any one track needs: this stage only has
# to avoid clipping, and the display expands each band's own range afterwards.
SPECTRUM_FLOOR_DB = -80.0
SPECTRUM_CEILING_DB = 45.0
# The display cannot show more than one frame per repaint, so analysing every
# PCM buffer (often 40-90 per second) only burns CPU on an always-on machine.
SPECTRUM_MIN_INTERVAL = 1 / 60

SAMPLE_DTYPES = {
    QAudioFormat.SampleFormat.UInt8: np.uint8,
    QAudioFormat.SampleFormat.Int16: np.int16,
    QAudioFormat.SampleFormat.Int32: np.int32,
    QAudioFormat.SampleFormat.Float: np.float32,
}


@lru_cache(maxsize=16)
def spectrum_bands(fft_size: int, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Window and band boundaries for one audio format.

    Both depend only on the format, so they are built once per source instead of
    on every buffer.  The boundaries are FFT bin offsets, which lets the band
    maxima be taken with a single ``reduceat`` instead of 48 masked scans.
    """
    window = np.hanning(fft_size).astype(np.float32)
    frequencies = np.fft.rfftfreq(fft_size, d=1 / max(1, sample_rate))
    # Stopping at 14 kHz keeps the bands over frequencies music actually uses;
    # the octave above it is empty on most encoded sources and only bought dead
    # columns at the right-hand end of the display.
    edges = np.geomspace(32, min(14_000, max(70, sample_rate / 2)), SPECTRUM_BANDS + 1)
    indices = np.clip(np.searchsorted(frequencies, edges), 0, frequencies.size - 1)
    return window, indices


class SpectrumAnalyzer:
    """Rolling-window spectrum for one player.

    Each player that has a visualiser gets its own, because the window and the
    rate-limit are state: sharing one between the music and the announcement
    would smear one signal's audio into the other's display.
    """

    def __init__(self) -> None:
        self.enabled = True
        self._window = np.zeros(SPECTRUM_WINDOW, dtype=np.float32)
        self._rate = 0
        self._last_at = 0.0

    def feed(self, buffer: QAudioBuffer) -> np.ndarray | None:
        """Take one PCM buffer; return normalised bands when a frame is due."""
        if not self.enabled or not buffer.isValid() or buffer.frameCount() == 0:
            return None
        moment = time.monotonic()
        audio_format = buffer.format()
        dtype = SAMPLE_DTYPES.get(audio_format.sampleFormat())
        if dtype is None:
            return None
        samples = np.frombuffer(buffer.constData(), dtype=dtype)
        channels = max(1, audio_format.channelCount())
        frames = samples.size // channels
        if frames < 1:
            return None
        block = samples[: frames * channels].reshape(frames, channels)
        if dtype is np.uint8:
            # 8-bit PCM is offset binary: 128 is silence, not near-full negative
            # scale.  Reading it as signed would add a large false DC component.
            mono = block.mean(axis=1, dtype=np.float32) / 127.5 - 1.0
        elif dtype is np.float32:
            mono = block.mean(axis=1, dtype=np.float32)
        else:
            mono = block.mean(axis=1, dtype=np.float32) / float(np.iinfo(dtype).max)
        rate = audio_format.sampleRate()
        if rate != self._rate:
            self._rate = rate
            self._window[:] = 0.0
        # Keep the most recent window's worth of audio, across buffer boundaries.
        if mono.size >= SPECTRUM_WINDOW:
            self._window = mono[-SPECTRUM_WINDOW:].astype(np.float32, copy=True)
        else:
            self._window = np.concatenate((self._window[mono.size:], mono.astype(np.float32)))
        if moment - self._last_at < SPECTRUM_MIN_INTERVAL:
            return None
        window, indices = spectrum_bands(SPECTRUM_WINDOW, rate)
        magnitude = np.abs(np.fft.rfft(self._window * window))
        # Root-mean-square across each band, rather than its single loudest bin:
        # a lone peak no longer speaks for the whole band. Where a band is
        # narrower than one bin, reduceat yields that shared bin, which is the
        # honest reading; zeroing it left the bass end of the display dark.
        power = np.add.reduceat(magnitude * magnitude, indices)[:SPECTRUM_BANDS]
        widths = np.maximum(np.diff(np.append(indices, magnitude.size))[:SPECTRUM_BANDS], 1)
        bands = np.sqrt(power / widths).astype(np.float32)
        db = 20 * np.log10(np.maximum(bands, 1e-8))
        # Straight, unclipped dB. A mapping that topped out at 0 dBFS sat below
        # the median band level of ordinary music, pinning most of the spectrum.
        self._last_at = moment
        return np.clip(
            (db - SPECTRUM_FLOOR_DB) / (SPECTRUM_CEILING_DB - SPECTRUM_FLOOR_DB), 0.0, 1.0
        ).astype(np.float32)


class AudioController(QObject):
    status_changed = Signal(str)
    music_track_changed = Signal(str)
    announcement_started = Signal()
    announcement_finished = Signal()
    error = Signal(str)
    spectrum_ready = Signal(object)
    announcement_spectrum_ready = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.music_analyzer = SpectrumAnalyzer()
        self.announcement_analyzer = SpectrumAnalyzer()
        self.music_output = QAudioOutput()
        self.music_player = QMediaPlayer()
        self.music_player.setAudioOutput(self.music_output)
        self.music_buffer_output = QAudioBufferOutput()
        self.music_buffer_output.audioBufferReceived.connect(self._analyze_music_buffer)
        self.music_player.setAudioBufferOutput(self.music_buffer_output)
        self.announcement_output = QAudioOutput()
        self.announcement_player = QMediaPlayer()
        self.announcement_player.setAudioOutput(self.announcement_output)
        self.announcement_buffer_output = QAudioBufferOutput()
        self.announcement_buffer_output.audioBufferReceived.connect(self._analyze_announcement_buffer)
        self.announcement_player.setAudioBufferOutput(self.announcement_buffer_output)
        self.music_files: list[Path] = []
        self.track_index = 0
        self.repeat_all = True
        self.shuffle = False
        # A bag, not a coin toss: every track plays once before any repeats.
        # Drawing at random each time replays the same song back to back often
        # enough that it reads as a fault rather than as shuffle.
        self._shuffle_bag: list[int] = []
        self._shuffle_history: list[int] = []
        # Consecutive tracks that failed to load, so a bad file is skipped but a
        # playlist of nothing playable does not spin forever.
        self._music_failures = 0
        self.paused_for_announcement = False
        self.announcement_pending = False
        self._music_ended_during_announcement = False
        # Which recording currently holds the announcement output, so the list
        # can show a meter on it. Tracked here because a queued ad starts from
        # inside this class, where the caller is no longer involved.
        self.current_announcement: Path | None = None
        # Whether the ad on air was started by hand. A scheduled ad is never cut
        # short by a manual one; a manual one is.
        self._current_is_manual = False
        # (path, started by hand) - queued ads keep that distinction too.
        self._announcement_queue: deque[tuple[Path, bool]] = deque()
        self.music_volume = 0.55
        self.announcement_volume = 0.85
        self.duck_music = True
        self.fade_duration_ms = 1_500
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(30)
        self._fade_timer.timeout.connect(self._fade_step)
        self._fade_from = self.music_volume
        self._fade_to = self.music_volume
        self._fade_progress = 0
        self._fade_steps = 1
        self._fade_done: Callable[[], None] | None = None
        self.music_output.setVolume(self.music_volume)
        self.announcement_output.setVolume(self.announcement_volume)
        self.music_player.mediaStatusChanged.connect(self._music_status)
        self.announcement_player.mediaStatusChanged.connect(self._announcement_status)
        self.music_player.errorOccurred.connect(self._music_error)
        self.announcement_player.errorOccurred.connect(self._announcement_error)

    def _analyze_music_buffer(self, buffer: QAudioBuffer) -> None:
        bands = self.music_analyzer.feed(buffer)
        if bands is not None:
            self.spectrum_ready.emit(bands)

    def _analyze_announcement_buffer(self, buffer: QAudioBuffer) -> None:
        bands = self.announcement_analyzer.feed(buffer)
        if bands is not None:
            self.announcement_spectrum_ready.emit(bands)

    def set_analysis_enabled(self, music: bool, announcement: bool | None = None) -> None:
        """Skip spectrum work entirely while a visualiser is not on screen."""
        self.music_analyzer.enabled = music
        self.announcement_analyzer.enabled = music if announcement is None else announcement

    def set_music(self, files: list[Path]) -> None:
        updated_files = [f for f in files if f.exists()]
        if updated_files == self.music_files:
            return
        current_track = self.music_files[self.track_index] if self.music_files and self.track_index < len(self.music_files) else None
        self.music_files = updated_files
        self.track_index = self.music_files.index(current_track) if current_track in self.music_files else 0
        # The bag holds positions, so any edit to the list invalidates it.
        self._shuffle_bag.clear()
        self._shuffle_history.clear()

    def configure(self, music_volume: float, announcement_volume: float, duck_music: bool, fade_duration_ms: int) -> None:
        self.music_volume = music_volume
        self.announcement_volume = announcement_volume
        self.duck_music = duck_music
        self.fade_duration_ms = fade_duration_ms
        self.announcement_output.setVolume(announcement_volume)
        if not self.paused_for_announcement:
            self.music_output.setVolume(music_volume)

    def set_announcement_volume(self, volume: float) -> None:
        self.announcement_volume = max(0.0, min(1.0, volume))
        self.announcement_output.setVolume(self.announcement_volume)

    def stop_announcement(self) -> None:
        """Cut the announcement short by hand, and bring the music back.

        Stopping the player alone never fires EndOfMedia, so the music would
        stay paused at zero volume; the queue is dropped too, or the next queued
        ad would start the moment this one is cut.
        """
        if self.current_announcement is None and not self._announcement_queue:
            return
        self._announcement_queue.clear()
        self.announcement_pending = False
        if self.announcement_player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self.announcement_player.stop()
        self._restore_music()

    def seek_announcement(self, position_ms: int) -> None:
        """Move the announcement's playhead, clamped to what is loaded."""
        duration = self.announcement_player.duration()
        position_ms = max(0, position_ms)
        if duration > 0:
            position_ms = min(position_ms, duration)
        self.announcement_player.setPosition(position_ms)

    def pause_announcement(self) -> None:
        """Hold the announcement where it is; the music stays parked behind it."""
        if self.announcement_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.announcement_player.pause()
            self.status_changed.emit("Announcement paused")

    def resume_announcement(self) -> None:
        if self.announcement_player.playbackState() == QMediaPlayer.PlaybackState.PausedState:
            self.announcement_player.play()
            self.status_changed.emit("Announcement playing")

    def set_music_volume(self, volume: float) -> None:
        """Change the live music level, including the level restored after an ad."""
        self.music_volume = max(0.0, min(1.0, volume))
        if not self.paused_for_announcement:
            self.music_output.setVolume(self.music_volume)

    def set_audio_device(self, device: QAudioDevice) -> None:
        self.music_output.setDevice(device)
        self.announcement_output.setDevice(device)

    def _fade_to_volume(self, volume: float, done: Callable[[], None] | None = None) -> None:
        self._fade_timer.stop()
        self._fade_from = self.music_output.volume()
        self._fade_to = volume
        self._fade_progress = 0
        self._fade_steps = max(1, self.fade_duration_ms // self._fade_timer.interval())
        self._fade_done = done
        self._fade_timer.start()

    def _fade_step(self) -> None:
        self._fade_progress += 1
        fraction = min(1, self._fade_progress / self._fade_steps)
        self.music_output.setVolume(self._fade_from + (self._fade_to - self._fade_from) * fraction)
        if fraction >= 1:
            self._fade_timer.stop()
            done = self._fade_done
            self._fade_done = None
            if done:
                done()

    def _music_takes_over(self) -> None:
        """Playing music by hand ends any announcement, on air or queued.

        Not _restore_music: that resumes and fades the music back in, and the
        caller is about to drive the music itself. This just clears the ad and
        undoes the fade-out so the music comes in at full level.
        """
        if self.current_announcement is None and not self._announcement_queue:
            return
        self._announcement_queue.clear()
        self.announcement_pending = False
        self.current_announcement = None
        if self.announcement_player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self.announcement_player.stop()
        self._fade_timer.stop()
        self.paused_for_announcement = False
        self._music_ended_during_announcement = False
        self.music_output.setVolume(self.music_volume)
        self.announcement_finished.emit()

    def play_music(self) -> None:
        if not self.music_files:
            self.status_changed.emit("No background music selected")
            return
        self._music_takes_over()
        if self.music_player.playbackState() == QMediaPlayer.PlaybackState.PausedState:
            self.music_player.play()
            self.status_changed.emit("Background music playing")
            return
        self._play_track()

    def _play_track(self) -> None:
        if not self.music_files:
            return
        # Next / previous / a double-click during an ad must not play under it.
        self._music_takes_over()
        track = self.music_files[self.track_index % len(self.music_files)]
        self.music_player.setSource(QUrl.fromLocalFile(str(track)))
        self.music_player.play()
        self.music_track_changed.emit(track.name)
        self.status_changed.emit("Background music playing")

    def set_shuffle(self, enabled: bool) -> None:
        self.shuffle = enabled
        self._shuffle_bag.clear()

    def _draw_from_bag(self) -> int:
        """Next position in the shuffle, refilling once every track has run."""
        if not self._shuffle_bag:
            bag = list(range(len(self.music_files)))
            random.shuffle(bag)
            # Don't open a fresh bag with the track that just finished.
            if len(bag) > 1 and bag[-1] == self.track_index:
                bag[0], bag[-1] = bag[-1], bag[0]
            self._shuffle_bag = bag
        return self._shuffle_bag.pop()

    def next_track(self) -> None:
        if not self.music_files:
            return
        if self.shuffle and len(self.music_files) > 1:
            self._shuffle_history.append(self.track_index)
            del self._shuffle_history[:-64]
            self.track_index = self._draw_from_bag()
            self._play_track()
            return
        if self.track_index >= len(self.music_files) - 1:
            if not self.repeat_all:
                self.music_player.stop()
                self.status_changed.emit("Background playlist finished")
                return
            self.track_index = 0
        else:
            self.track_index += 1
        self._play_track()

    def previous_track(self) -> None:
        if not self.music_files:
            return
        if self.shuffle and self._shuffle_history:
            # Back through what actually played, not back one position in a
            # list order the shuffle is not following.
            self.track_index = self._shuffle_history.pop()
        else:
            self.track_index = (self.track_index - 1) % len(self.music_files)
        self._play_track()

    def play_music_index(self, index: int) -> None:
        """Start a particular item selected from the playlist."""
        if not self.music_files:
            return
        self.track_index = max(0, min(index, len(self.music_files) - 1))
        self._play_track()

    def seek(self, position_ms: int) -> None:
        """Move the playhead, clamped to what is actually loaded."""
        duration = self.music_player.duration()
        position_ms = max(0, position_ms)
        if duration > 0:
            position_ms = min(position_ms, duration)
        self.music_player.setPosition(position_ms)

    def pause_music(self) -> None:
        self.music_player.pause()
        # Pausing by hand during an announcement is a deliberate override, so
        # the announcement must not start the music again when it ends.
        self.paused_for_announcement = False
        self._music_ended_during_announcement = False
        self.status_changed.emit("Background music paused")

    def queued_announcements(self) -> int:
        return len(self._announcement_queue)

    def play_announcement(self, path: Path, manual: bool = False) -> bool:
        if not path.exists():
            return False
        if manual:
            on_air = self.current_announcement is not None
            if on_air and not self._current_is_manual:
                # A scheduled ad is on air. It is not cut short for a hand-played
                # one: that would be a person interfering with the schedule. The
                # manual ad goes out right after it, ahead of anything else queued.
                self._announcement_queue.appendleft((path, True))
                return True
            # A manual ad on air is replaced - that is what pressing play means.
            # The music is treated exactly as for a scheduled ad: held behind the
            # announcement, and handed back when it ends.
            self._announcement_queue = deque(entry for entry in self._announcement_queue if not entry[1])
            if self.announcement_player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
                self.announcement_player.stop()
            self.announcement_pending = False
            if self.paused_for_announcement:
                # The music is already parked behind the ad being replaced;
                # keep that so it still comes back after the new one.
                self.announcement_pending = True
                self._start_announcement(path, manual=True)
                return True
        if self.announcement_pending or self.announcement_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            # Another ad holds the output.  Two schedules landing in the same
            # minute is ordinary, and the second announcement still has to be
            # heard, so queue it instead of discarding it.
            if len(self._announcement_queue) >= MAX_QUEUED_ANNOUNCEMENTS:
                return False
            self._announcement_queue.append((path, manual))
            return True
        self.announcement_pending = True
        self.paused_for_announcement = self.music_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if self.paused_for_announcement:
            # Announcements have exclusive use of the output.  Fade the music
            # out, pause it at its current position, then play exactly one voice
            # recording.  It will resume after the announcement finishes.
            self._fade_to_volume(0.0, lambda: self._pause_music_and_start_announcement(path, manual))
            self.status_changed.emit("Background music pausing for announcement")
            return True
        self._start_announcement(path, manual)
        return True

    def _pause_music_and_start_announcement(self, path: Path, manual: bool = False) -> None:
        self.music_player.pause()
        self._start_announcement(path, manual)

    def _start_announcement(self, path: Path, manual: bool = False) -> None:
        self.current_announcement = path
        self._current_is_manual = manual
        self.announcement_player.setSource(QUrl.fromLocalFile(str(path)))
        self.announcement_player.play()
        self.status_changed.emit("Announcement playing")
        # An ad usually starts after the music has faded out, well after the
        # caller asked for it, so the list is told when it actually begins.
        self.announcement_started.emit()

    def _music_error(self, _: QMediaPlayer.Error, text: str) -> None:
        """Skip a file the decoder rejects, instead of stopping the playlist.

        An unattended install cannot have one corrupt download turn into silence
        until somebody notices; the player just stops on it and stays stopped.
        """
        self.error.emit(f"Music: {text}")
        self._music_failures += 1
        if self._music_failures >= max(1, len(self.music_files)):
            self.status_changed.emit("No playable music")
            return
        # Deferred: this runs inside the failing setSource/play, and the player
        # should not be re-sourced from within its own error signal.
        QTimer.singleShot(0, self.next_track)

    def _music_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self._music_failures = 0
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        if self.paused_for_announcement:
            # The track ran out during the fade-out, before the pause landed.
            # Advancing now would play over the ad, so it waits for the restore.
            self._music_ended_during_announcement = True
            return
        self.next_track()

    def _announcement_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        self.announcement_pending = False
        if self._start_next_announcement():
            return
        self._restore_music()

    def _start_next_announcement(self) -> bool:
        """Chain straight into the next queued ad, leaving the music paused."""
        if not self._announcement_queue:
            return False
        self.announcement_pending = True
        path, manual = self._announcement_queue.popleft()
        self._start_announcement(path, manual)
        return True

    def _restore_music(self) -> None:
        self.current_announcement = None
        self._current_is_manual = False
        if self.paused_for_announcement:
            if self._music_ended_during_announcement:
                # Resuming a finished file plays nothing, so carry on with the
                # next track instead of leaving the playlist stalled.
                self.next_track()
            elif self.music_player.playbackState() == QMediaPlayer.PlaybackState.PausedState:
                self.music_player.play()
            if self.duck_music:
                self._fade_to_volume(self.music_volume)
            else:
                self.music_output.setVolume(self.music_volume)
        self.paused_for_announcement = False
        self._music_ended_during_announcement = False
        self._emit_music_status()
        self.announcement_finished.emit()

    def _emit_music_status(self) -> None:
        """Report the real music state; a preview with no playlist is not on air."""
        state = self.music_player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.status_changed.emit("Background music playing")
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self.status_changed.emit("Background music paused")
        else:
            self.status_changed.emit("Background music stopped")

    def _announcement_error(self, _: QMediaPlayer.Error, text: str) -> None:
        # A bad file must not strand the music paused at zero volume, and must
        # not swallow the ads queued behind it.
        self.announcement_pending = False
        if not self._start_next_announcement():
            self._restore_music()
        self.error.emit(f"Announcement: {text}")
