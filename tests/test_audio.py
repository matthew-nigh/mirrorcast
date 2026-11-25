import io

from mirrorcast.audio_loopback import SAMPLE_RATE, LoopbackAudio, pad_frames_needed


def test_no_padding_when_on_time():
    assert pad_frames_needed(1.0, SAMPLE_RATE) == 0
    assert pad_frames_needed(1.0, SAMPLE_RATE - 100) == 0


def test_padding_when_audio_stalls():
    pad = pad_frames_needed(2.0, SAMPLE_RATE)  # one second behind
    assert SAMPLE_RATE * 0.9 < pad <= SAMPLE_RATE


def test_write_survives_closed_pipe():
    sink = io.BytesIO()
    a = LoopbackAudio(sink, log=lambda m: None)
    assert a._write(b"abc")
    sink.close()
    assert a._write(b"abc") is False
