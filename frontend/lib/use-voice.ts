"use client";

import { useState, useRef, useCallback, useEffect } from "react";

// ─── Speech-to-Text ───

export type SttError =
  | "not_supported"       // Browser doesn't have Web Speech API
  | "insecure_context"    // Page is served over plain HTTP (not HTTPS/localhost)
  | "permission_denied"   // User denied mic permission
  | "no_speech"           // Mic opened but nothing was heard
  | "audio_capture"       // Mic hardware unavailable
  | "network"             // Network error reaching the speech service
  | "aborted"             // Recognition was aborted unexpectedly
  | "unknown";

export function sttErrorMessage(err: SttError): string {
  switch (err) {
    case "not_supported":
      return "Voice input isn't supported in this browser. Try Chrome or Edge.";
    case "insecure_context":
      return "Voice input requires a secure connection (HTTPS). Use the https:// URL.";
    case "permission_denied":
      return "Microphone permission denied. Allow mic access in your browser settings and try again.";
    case "no_speech":
      return "I didn't hear anything. Try again and speak clearly.";
    case "audio_capture":
      return "No microphone found. Check that one is connected.";
    case "network":
      return "Voice service unreachable. Check your internet connection.";
    case "aborted":
      return "Voice input was interrupted. Try again.";
    default:
      return "Voice input failed. Try again or type your answer.";
  }
}

interface UseSpeechToTextReturn {
  listening: boolean;
  supported: boolean;
  transcript: string;
  error: SttError | null;
  start: () => void;
  stop: () => void;
  toggle: () => void;
  clearError: () => void;
}

const SILENCE_DELAY_MS = 3000; // Auto-send after 3s of silence

function mapErrorCode(code: string | undefined): SttError {
  switch (code) {
    case "not-allowed":
    case "service-not-allowed":
      return "permission_denied";
    case "no-speech":
      return "no_speech";
    case "audio-capture":
      return "audio_capture";
    case "network":
      return "network";
    case "aborted":
      return "aborted";
    default:
      return "unknown";
  }
}

export function useSpeechToText(onResult: (text: string) => void): UseSpeechToTextReturn {
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState("");
  const [error, setError] = useState<SttError | null>(null);
  const recognitionRef = useRef<any>(null);
  const silenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const transcriptRef = useRef("");
  const userStoppedRef = useRef(false); // Distinguish user-stop vs auto-end
  const onResultRef = useRef(onResult);
  onResultRef.current = onResult;
  const supported = typeof window !== "undefined" && ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);

  const clearSilenceTimer = useCallback(() => {
    if (silenceTimerRef.current) {
      clearTimeout(silenceTimerRef.current);
      silenceTimerRef.current = null;
    }
  }, []);

  const clearError = useCallback(() => setError(null), []);

  const stop = useCallback(() => {
    userStoppedRef.current = true;
    clearSilenceTimer();
    if (recognitionRef.current) {
      try { recognitionRef.current.stop(); } catch {}
      recognitionRef.current = null;
    }
    const text = transcriptRef.current.trim();
    if (text) {
      onResultRef.current(text);
      transcriptRef.current = "";
      setTranscript("");
    }
    setListening(false);
  }, [clearSilenceTimer]);

  const start = useCallback(() => {
    // Surface specific errors BEFORE trying to start so the user sees a reason.
    if (!supported) {
      setError("not_supported");
      return;
    }
    // Web Speech API requires a secure context. Plain HTTP (e.g. raw VPS IP)
    // silently fails — surface this up so the UI can show a clear message.
    if (typeof window !== "undefined" && !window.isSecureContext) {
      setError("insecure_context");
      return;
    }

    setError(null);
    userStoppedRef.current = false;

    // Tear down any prior instance — but don't fire onResult via the
    // normal stop() path since we're about to restart.
    if (recognitionRef.current) {
      try { recognitionRef.current.stop(); } catch {}
      recognitionRef.current = null;
    }
    clearSilenceTimer();
    transcriptRef.current = "";
    setTranscript("");

    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    const recognition = new SpeechRecognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = "en-US";

    recognition.onresult = (event: any) => {
      let full = "";
      for (let i = 0; i < event.results.length; i++) {
        full += event.results[i][0].transcript;
      }
      transcriptRef.current = full;
      setTranscript(full);
      // A successful result means the previous transient error (often a
      // harmless "network" blip) is no longer meaningful — clear it so the
      // banner doesn't linger over working speech.
      setError(null);

      clearSilenceTimer();
      silenceTimerRef.current = setTimeout(() => {
        userStoppedRef.current = true; // Auto-send = intentional stop
        const text = transcriptRef.current.trim();
        if (text) {
          onResultRef.current(text);
          transcriptRef.current = "";
          setTranscript("");
        }
        if (recognitionRef.current) {
          try { recognitionRef.current.stop(); } catch {}
          recognitionRef.current = null;
        }
        setListening(false);
      }, SILENCE_DELAY_MS);
    };

    recognition.onerror = (event: any) => {
      const code = event?.error as string | undefined;
      const mapped = mapErrorCode(code);
      // "no-speech" fires routinely on short pauses in some browsers.
      // "network" and "aborted" fire on transient disconnects in the speech
      // service mid-stream — Chrome auto-recovers and we usually still get
      // a transcript. Only surface these to the user if we truly have
      // NOTHING captured AND the user hasn't intentionally stopped.
      const shouldSuppress =
        (mapped === "no_speech" || mapped === "network" || mapped === "aborted")
        && (transcriptRef.current || !userStoppedRef.current);
      if (shouldSuppress) {
        return;
      }
      // Log for debugging — mic failures are otherwise invisible.
      // eslint-disable-next-line no-console
      console.warn("[speech-to-text] error:", code, event);
      setError(mapped);
      clearSilenceTimer();
      setListening(false);
    };

    recognition.onend = () => {
      clearSilenceTimer();
      // Chrome auto-ends after ~15s of silence. If the user didn't stop
      // manually and we have no transcript, restart transparently so the
      // mic stays "live" until the user clicks off.
      if (!userStoppedRef.current && !transcriptRef.current.trim()) {
        try {
          const rec = recognitionRef.current;
          if (rec) {
            rec.start();
            return;
          }
        } catch {
          // Fall through to stop if restart fails (e.g. quota exceeded)
        }
      }
      const text = transcriptRef.current.trim();
      if (text) {
        onResultRef.current(text);
        transcriptRef.current = "";
        setTranscript("");
      }
      setListening(false);
      recognitionRef.current = null;
    };

    recognitionRef.current = recognition;
    try {
      recognition.start();
      setListening(true);
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("[speech-to-text] failed to start:", e);
      setError("unknown");
      setListening(false);
      recognitionRef.current = null;
    }
  }, [supported, clearSilenceTimer]);

  const toggle = useCallback(() => {
    if (listening) stop();
    else start();
  }, [listening, start, stop]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      clearSilenceTimer();
      if (recognitionRef.current) {
        try { recognitionRef.current.stop(); } catch {}
      }
    };
  }, [clearSilenceTimer]);

  return { listening, supported, transcript, error, start, stop, toggle, clearError };
}

// ─── Text-to-Speech ───

interface UseTTSReturn {
  speaking: boolean;
  supported: boolean;
  enabled: boolean;
  setEnabled: (v: boolean) => void;
  speak: (text: string) => void;
  stop: () => void;
}

const TTS_KEY = "aria_tts_enabled";

export function useTTS(): UseTTSReturn {
  const [speaking, setSpeaking] = useState(false);
  const [enabled, setEnabledState] = useState(() => {
    if (typeof window === "undefined") return true;
    const stored = localStorage.getItem(TTS_KEY);
    return stored === null ? true : stored === "true";
  });
  const supported = typeof window !== "undefined" && "speechSynthesis" in window;

  const setEnabled = useCallback((v: boolean) => {
    setEnabledState(v);
    if (typeof window !== "undefined") localStorage.setItem(TTS_KEY, String(v));
    if (!v && supported) window.speechSynthesis.cancel();
  }, [supported]);

  const stop = useCallback(() => {
    if (supported) window.speechSynthesis.cancel();
    setSpeaking(false);
  }, [supported]);

  const speak = useCallback((text: string) => {
    if (!supported || !text) return;
    stop();

    // Strip markdown/HTML for cleaner speech
    const clean = text
      .replace(/#{1,6}\s/g, "")
      .replace(/\*{1,2}(.*?)\*{1,2}/g, "$1")
      .replace(/`{1,3}[^`]*`{1,3}/g, "")
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/<[^>]+>/g, "")
      .replace(/\n{2,}/g, ". ")
      .replace(/\n/g, " ")
      .trim();

    const utterance = new SpeechSynthesisUtterance(clean);
    utterance.rate = 1.05;
    utterance.pitch = 1;

    // Try to pick a natural-sounding voice
    const voices = window.speechSynthesis.getVoices();
    const preferred = voices.find(v => v.name.includes("Google") && v.lang.startsWith("en")) ||
      voices.find(v => v.lang.startsWith("en") && v.localService === false) ||
      voices.find(v => v.lang.startsWith("en"));
    if (preferred) utterance.voice = preferred;

    utterance.onstart = () => setSpeaking(true);
    utterance.onend = () => setSpeaking(false);
    utterance.onerror = () => setSpeaking(false);

    window.speechSynthesis.speak(utterance);
  }, [supported, stop]);

  // Cleanup on unmount
  useEffect(() => {
    return () => { if (supported) window.speechSynthesis.cancel(); };
  }, [supported]);

  return { speaking, supported, enabled, setEnabled, speak, stop };
}
