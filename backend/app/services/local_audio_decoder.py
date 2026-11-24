"""Isolated local-audio decoder. Writes only canonical mono 16 kHz PCM WAV."""
import io
from pathlib import Path
import sys
import wave

import av

RATE = 16000
FORMATS = {".wav":"wav", ".ogg":"ogg", ".mp3":"mp3", ".m4a":"mov", ".flac":"flac", ".webm":"matroska"}


def deny_resource(*args):
    raise OSError("External resources are disabled")


def convert(source, output, suffix, max_seconds):
    # Use a bounded byte stream and deny nested inputs (e.g. MOV data references).
    sample_count = 0
    options = {"protocol_whitelist":"pipe", "enable_drefs":"0", "use_absolute_path":"0"}
    with av.open(io.BytesIO(source.read_bytes()), mode="r", format=FORMATS[suffix],
                 options=options, io_open=deny_resource) as container:
        if len(container.streams)!=1 or len(container.streams.audio)!=1:
            raise ValueError("Expected one audio stream")
        if container.duration and container.duration > max_seconds*av.time_base:
            return 3
        stream = container.streams.audio[0]
        codec = stream.codec_context
        codec.thread_count = 1
        if codec.sample_rate > 192000 or (codec.layout and len(codec.layout.channels)>8):
            raise ValueError("Invalid audio format")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        with wave.open(str(output),"wb") as wav:
            wav.setparams((1,2,RATE,0,"NONE","not compressed"))
            def write(frames):
                nonlocal sample_count
                for frame in frames:
                    sample_count += frame.samples
                    if sample_count > max_seconds*RATE:
                        raise OverflowError
                    wav.writeframesraw(frame.to_ndarray().astype("<i2",copy=False).tobytes())
            try:
                for frame in container.decode(stream):
                    if frame.sample_rate > 192000 or len(frame.layout.channels)>8:
                        raise ValueError("Invalid audio frame")
                    write(resampler.resample(frame))
                write(resampler.resample(None))
            except OverflowError:
                return 3
    return 0 if sample_count else 2


if __name__ == "__main__":
    try:
        status = convert(Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3],int(sys.argv[4]))
    except (OSError, ValueError, KeyError, RuntimeError):
        status = 2
    sys.exit(status)
