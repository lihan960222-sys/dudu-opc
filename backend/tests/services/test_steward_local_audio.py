import io
import struct
import wave
import pytest

from app.services import audio_ingestion as audio, local_whisper
from app.services.attachments import AttachmentError


def fixture_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1,2,16000,16000,"NONE","not compressed"))
        wav.writeframes(struct.pack("<h",300)*16000)
    return output.getvalue()


@pytest.mark.asyncio
async def test_private_local_model_uses_bundled_decoder_without_cli(monkeypatch,tmp_path):
    monkeypatch.setenv("STEWARD_WHISPER_MODEL_DIR",str(tmp_path))
    for name in ("config.json","model.bin","tokenizer.json","vocabulary.txt"):
        (tmp_path/name).write_bytes(b"fixture")
    monkeypatch.setattr(audio.shutil,"which",lambda _name:None)
    def transcribed(data,name):
        with wave.open(io.BytesIO(data)) as wav:
            assert (wav.getnchannels(),wav.getframerate(),wav.getsampwidth(),wav.getnframes()) == (1,16000,2,16000)
        return {"text":"spoken example","segments":[],"provider":"local","model":"tiny"}
    monkeypatch.setattr(local_whisper,"transcribe_bytes",transcribed)
    monkeypatch.setattr(audio.settings,"AUDIO_TRANSCRIPTION_API_KEY","synthetic-unused")
    assert audio.capabilities()["provider"] == "local"
    assert audio.capabilities()["local_available"]
    with pytest.raises(AttachmentError,match="locally"):
        await audio.transcribe("sample.wav",fixture_wav(),"openai")
    assert (await audio.transcribe("sample.wav",fixture_wav(),"local"))["text"] == "spoken example"
    with pytest.raises(AttachmentError,match="corrupt"):
        await audio.transcribe("sample.wav",b"not a wave","local")
    (tmp_path/"model.bin").unlink()
    assert not audio.capabilities()["local_available"]
    with pytest.raises(AttachmentError):
        await audio.transcribe("sample.wav",fixture_wav(),"local")


def test_local_audio_rejects_non_audio_and_limits_duration(tmp_path,monkeypatch):
    import av
    import threading
    out=io.BytesIO()
    with av.open(out,mode="w",format="mp4") as clip:
        stream=clip.add_stream("mpeg4",rate=1)
        stream.width=stream.height=16
        for packet in stream.encode(av.VideoFrame(16,16,"yuv420p")):
            clip.mux(packet)
        for packet in stream.encode():
            clip.mux(packet)
    with pytest.raises(AttachmentError,match="corrupt"):
        audio._private_local_wav("music.m4a",out.getvalue(),threading.Event())
    monkeypatch.setattr(audio,"MAX_SECONDS",0)
    with pytest.raises(AttachmentError) as limit:
        audio._private_local_wav("sample.wav",fixture_wav(),threading.Event())
    assert limit.value.code == "audio_too_long"
