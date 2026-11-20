import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorText, isAbort } from '../api';
import type { VoiceCapabilities } from '../types';
type Capture = { stream: MediaStream; context: AudioContext; source: MediaStreamAudioSourceNode; processor: ScriptProcessorNode; mute: GainNode; chunks: Float32Array[]; length: number; timer: ReturnType<typeof setTimeout> | null; sampleRate: number };

// Average the input samples within each output interval to make a mono 16 kHz PCM WAV.
export function encodeWav(chunks: Float32Array[], inputRate: number): ArrayBuffer {
  const length = chunks.reduce((total, chunk) => total + chunk.length, 0);
  const samples = new Float32Array(length); let offset = 0;
  for (const chunk of chunks) { samples.set(chunk, offset); offset += chunk.length; }
  const count = Math.min(192000, Math.floor(length * 16000 / inputRate));
  const output = new ArrayBuffer(44 + count * 2); const view = new DataView(output);
  const word = (start: number, value: string) => { for (let i = 0; i < value.length; i++) view.setUint8(start + i, value.charCodeAt(i)); };
  word(0, 'RIFF'); view.setUint32(4, 36 + count * 2, true); word(8, 'WAVE'); word(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true); word(36, 'data'); view.setUint32(40, count * 2, true);
  for (let i = 0; i < count; i++) { const from = Math.floor(i * inputRate / 16000); const to = Math.min(length, Math.max(from + 1, Math.floor((i + 1) * inputRate / 16000))); let sum = 0; for (let j = from; j < to; j++) sum += samples[j]; const value = Math.max(-1, Math.min(1, sum / (to - from))); view.setInt16(44 + i * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true); }
  return output;
}
function release(capture: Capture | null) { if (!capture) return; if (capture.timer) clearTimeout(capture.timer); capture.processor.onaudioprocess = null; capture.stream.getTracks().forEach(track => track.stop()); capture.source.disconnect(); capture.processor.disconnect(); capture.mute.disconnect(); void capture.context.close().catch(() => undefined); }
export function useVoice(onTranscript: (text: string) => void) {
  const [capabilities, setCapabilities] = useState<VoiceCapabilities | null>(null);
  const [phase, setPhase] = useState<'idle' | 'requesting' | 'recording' | 'transcribing'>('idle');
  const [error, setError] = useState('');
  const captureRef = useRef<Capture | null>(null); const epoch = useRef(0); const abortRef = useRef<AbortController | null>(null); const callback = useRef(onTranscript); callback.current = onTranscript;
  useEffect(() => { const controller = new AbortController(); void api<VoiceCapabilities>('/voice/capabilities', { signal: controller.signal }).then(setCapabilities).catch(error => { if (!isAbort(error)) setCapabilities({ available: false, engine: 'Local Windows speech', languages: [], detail: errorText(error) }); }); return () => controller.abort(); }, []);
  const cancel = useCallback(() => { epoch.current++; release(captureRef.current); captureRef.current = null; abortRef.current?.abort(); setPhase('idle'); }, []);
  useEffect(() => () => { epoch.current++; release(captureRef.current); captureRef.current = null; abortRef.current?.abort(); }, []);
  const stop = useCallback(async () => {
    const capture = captureRef.current; if (!capture) return;
    const current = epoch.current; captureRef.current = null; release(capture); setPhase('transcribing');
    const controller = new AbortController(); abortRef.current = controller;
    try { const wav = encodeWav(capture.chunks, capture.sampleRate); if (wav.byteLength <= 44) throw new Error('No audio was captured. Try speaking after the recording indicator appears.'); if (wav.byteLength > 512 * 1024) throw new Error('The recording is too long. Please try a shorter phrase.'); const result = await api<{ text: string; confidence: number; engine: string }>('/voice/transcribe', { method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: wav, signal: controller.signal }); if (current === epoch.current) { if (!result.text.trim()) throw new Error('No speech was recognized. You can try again or type your message.'); callback.current(result.text); } }
    catch (error) { if (current === epoch.current && !isAbort(error)) setError(errorText(error)); }
    finally { if (current === epoch.current) setPhase('idle'); }
  }, []);
  const start = useCallback(async () => {
    if (captureRef.current) return; const current = ++epoch.current; setError(''); setPhase('requesting'); let stream: MediaStream | null = null; let context: AudioContext | null = null;
    try { if (!navigator.mediaDevices?.getUserMedia) throw new Error('Microphone capture is unavailable in this browser. Please type your message.'); stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false }); if (current !== epoch.current) { stream.getTracks().forEach(track => track.stop()); return; } context = new AudioContext(); await context.resume(); if (current !== epoch.current) { stream.getTracks().forEach(track => track.stop()); await context.close(); return; } const source = context.createMediaStreamSource(stream); const processor = context.createScriptProcessor(4096, 1, 1); const mute = context.createGain(); mute.gain.value = 0; const capture: Capture = { stream, context, source, processor, mute, chunks: [], length: 0, timer: null, sampleRate: context.sampleRate }; captureRef.current = capture; processor.onaudioprocess = event => { if (captureRef.current !== capture) return; const remaining = Math.max(0, Math.floor(capture.sampleRate * 12) - capture.length); const chunk = event.inputBuffer.getChannelData(0).slice(0, remaining); capture.chunks.push(chunk); capture.length += chunk.length; if (capture.length >= capture.sampleRate * 12) void stop(); }; source.connect(processor); processor.connect(mute); mute.connect(context.destination); capture.timer = setTimeout(() => void stop(), 12000); setPhase('recording'); }
    catch (error) { stream?.getTracks().forEach(track => track.stop()); if (context && context.state !== 'closed') void context.close(); if (current === epoch.current) { setError(error instanceof DOMException && error.name === 'NotAllowedError' ? 'Microphone permission was not granted. You can still type your message.' : errorText(error)); setPhase('idle'); } }
  }, [stop]);
  return { capabilities, phase, error, start, stop, cancel };
}

export function useLocalSpeech(rate: number) {
  const [available, setAvailable] = useState(false); const [speaking, setSpeaking] = useState(false); const [message, setMessage] = useState('');
  const voices = useRef<SpeechSynthesisVoice[]>([]);
  useEffect(() => { if (!('speechSynthesis' in window)) return; const update = () => { voices.current = window.speechSynthesis.getVoices().filter(voice => voice.localService); setAvailable(voices.current.length > 0); }; update(); window.speechSynthesis.addEventListener('voiceschanged', update); return () => { window.speechSynthesis.removeEventListener('voiceschanged', update); window.speechSynthesis.cancel(); }; }, []);
  const stop = useCallback(() => { if ('speechSynthesis' in window) window.speechSynthesis.cancel(); setSpeaking(false); }, []);
  const speak = useCallback((text: string) => { if (!voices.current.length) { setMessage('No local browser voice is available. Text remains available.'); return; } window.speechSynthesis.cancel(); const utterance = new SpeechSynthesisUtterance(text); utterance.voice = voices.current.find(voice => voice.lang.startsWith('en')) || voices.current[0]; utterance.rate = rate; utterance.onstart = () => setSpeaking(true); utterance.onend = () => setSpeaking(false); utterance.onerror = () => setSpeaking(false); setMessage(''); window.speechSynthesis.speak(utterance); }, [rate]);
  return { available, speaking, message, speak, stop };
}
