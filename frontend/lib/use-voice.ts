"use client";

import { useState, useRef, useCallback, useEffect } from "react";

// ─── Speech-to-Text ───

interface UseSpeechToTextReturn {
  listening: boolean;
  supported: boolean;
  start: () => void;
  stop: () => void;
  toggle: () => void;
}

export function useSpeechToText(onResult: (text: string) => void): UseSpeechToTextReturn {
  const [listening, setListening] = useState(false);
  const recognitionRef = useRef<any>(null);
  const supported = typeof window !== "undefined" && ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);

  const stop = useCallback(() => {
    if (recognitionRef.current) {
      recognitionRef.current.stop();
      recognitionRef.current = null;
    }
    setListening(false);
  }, []);

  const start = useCallback(() => {
    if (!supported) return;
    stop();

    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    const recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = "en-US";

    recognition.onresult = (event: any) => {
      const transcript = event.results[0][0].transcript;
      if (transcript) onResult(transcript);
    };

    recognition.onerror = () => { setListening(false); };
    recognition.onend = () => { setListening(false); recognitionRef.current = null; };

    recognitionRef.current = recognition;
    recognition.start();
    setListening(true);
  }, [supported, onResult, stop]);

  const toggle = useCallback(() => {
    if (listening) stop();
    else start();
  }, [listening, start, stop]);

  // Cleanup on unmount
  useEffect(() => {
    return () => { if (recognitionRef.current) recognitionRef.current.stop(); };
  }, []);

  return { listening, supported, start, stop, toggle };
}

// ─── Text-to-Speech ───

interface UseTTSReturn {
  speaking: boolean;
  supported: boolean;
  speak: (text: string) => void;
  stop: () => void;
}

export function useTTS(): UseTTSReturn {
  const [speaking, setSpeaking] = useState(false);
  const supported = typeof window !== "undefined" && "speechSynthesis" in window;

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

  return { speaking, supported, speak, stop };
}
