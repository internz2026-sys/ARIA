"use client";

import { useState, useRef, useCallback, useEffect } from "react";

// ─── Speech-to-Text ───

interface UseSpeechToTextReturn {
  listening: boolean;
  supported: boolean;
  transcript: string;
  start: () => void;
  stop: () => void;
  toggle: () => void;
}

const SILENCE_DELAY_MS = 5000; // Wait 5s of silence before auto-sending

export function useSpeechToText(onResult: (text: string) => void): UseSpeechToTextReturn {
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState("");
  const recognitionRef = useRef<any>(null);
  const silenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const transcriptRef = useRef("");
  const onResultRef = useRef(onResult);
  onResultRef.current = onResult;
  const supported = typeof window !== "undefined" && ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);

  const clearSilenceTimer = useCallback(() => {
    if (silenceTimerRef.current) {
      clearTimeout(silenceTimerRef.current);
      silenceTimerRef.current = null;
    }
  }, []);

  const stop = useCallback(() => {
    clearSilenceTimer();
    if (recognitionRef.current) {
      recognitionRef.current.stop();
      recognitionRef.current = null;
    }
    // Send accumulated transcript on manual stop
    const text = transcriptRef.current.trim();
    if (text) {
      onResultRef.current(text);
      transcriptRef.current = "";
      setTranscript("");
    }
    setListening(false);
  }, [clearSilenceTimer]);

  const start = useCallback(() => {
    if (!supported) return;
    stop();
    transcriptRef.current = "";
    setTranscript("");

    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    const recognition = new SpeechRecognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = "en-US";

    recognition.onresult = (event: any) => {
      // Build full transcript from all results
      let full = "";
      for (let i = 0; i < event.results.length; i++) {
        full += event.results[i][0].transcript;
      }
      transcriptRef.current = full;
      setTranscript(full);

      // Reset the silence timer on every new speech input
      clearSilenceTimer();
      silenceTimerRef.current = setTimeout(() => {
        // 5s of silence — auto-send and stop
        const text = transcriptRef.current.trim();
        if (text) {
          onResultRef.current(text);
          transcriptRef.current = "";
          setTranscript("");
        }
        if (recognitionRef.current) {
          recognitionRef.current.stop();
          recognitionRef.current = null;
        }
        setListening(false);
      }, SILENCE_DELAY_MS);
    };

    recognition.onerror = () => { clearSilenceTimer(); setListening(false); };
    recognition.onend = () => {
      // If ended unexpectedly (browser limit), send what we have
      clearSilenceTimer();
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
    recognition.start();
    setListening(true);
  }, [supported, stop, clearSilenceTimer]);

  const toggle = useCallback(() => {
    if (listening) stop();
    else start();
  }, [listening, start, stop]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      clearSilenceTimer();
      if (recognitionRef.current) recognitionRef.current.stop();
    };
  }, [clearSilenceTimer]);

  return { listening, supported, transcript, start, stop, toggle };
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
