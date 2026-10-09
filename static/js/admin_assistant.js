(() => {
  const assistant = document.querySelector("[data-admin-assistant]");
  if (!assistant) return;

  const toggle = assistant.querySelector("[data-assistant-toggle]");
  const close = assistant.querySelector("[data-assistant-close]");
  const panel = assistant.querySelector("[data-assistant-panel]");
  const form = assistant.querySelector("[data-assistant-form]");
  const input = assistant.querySelector("[data-assistant-question]");
  const messages = assistant.querySelector("[data-assistant-messages]");
  const submit = assistant.querySelector("[data-assistant-submit]");
  const mic = assistant.querySelector("[data-assistant-mic]");
  const status = assistant.querySelector("[data-assistant-status]");
  const csrfToken = assistant.querySelector("[data-assistant-csrf]").value;
  const wakePreferenceKey = "accemAssistantWakeWordEnabled";
  let recognition = null;
  let isListening = false;
  let isStarting = false;
  let recognitionFailed = false;
  let wakeWordEnabled = false;
  let wakePhraseHeard = false;
  let wakeResultIndex = null;
  let commandFinalTranscript = "";
  let commandInterimTranscript = "";
  let submitTimer = null;
  let restartTimer = null;
  let assistantBusy = false;
  let isSpeaking = false;
  let speechVoices = window.speechSynthesis?.getVoices() || [];
  const preferredVoiceNames = [
    "samantha", "ava", "karen", "kathy", "moira", "victoria",
    "fiona", "tessa", "allison", "susan", "zoe", "lekha",
  ];

  window.speechSynthesis?.addEventListener("voiceschanged", () => {
    speechVoices = window.speechSynthesis.getVoices();
  });

  const selectFeminineVoice = () => {
    const voices = speechVoices.length ? speechVoices : window.speechSynthesis.getVoices();
    const englishVoices = voices.filter((voice) => /^en(?:-|$)/i.test(voice.lang));
    for (const preferredName of preferredVoiceNames) {
      const voice = englishVoices.find((candidate) =>
        candidate.name.toLowerCase().startsWith(preferredName)
      );
      if (voice) return voice;
    }
    return englishVoices.find((voice) => voice.default) || englishVoices[0] || null;
  };

  const addMessage = (kind, text) => {
    const message = document.createElement("article");
    message.className = `admin-assistant__message admin-assistant__message--${kind}`;
    const body = document.createElement("p");
    body.textContent = text;
    message.append(body);
    messages.append(message);
    messages.scrollTop = messages.scrollHeight;
  };

  const updateWakeButton = () => {
    mic.classList.toggle("is-listening", wakeWordEnabled);
    mic.setAttribute("aria-pressed", String(wakeWordEnabled));
    mic.setAttribute(
      "aria-label",
      wakeWordEnabled ? "Disable Hey Mira wake word" : "Enable Hey Mira wake word"
    );
    mic.querySelector("span").textContent = wakeWordEnabled ? "Hey Mira on" : "Enable Hey Mira";
    toggle.querySelector("span").textContent = wakeWordEnabled ? "Mira is listening" : "Ask Mira";
  };

  const persistWakePreference = () => {
    try {
      if (wakeWordEnabled) {
        window.localStorage.setItem(wakePreferenceKey, "true");
      } else {
        window.localStorage.removeItem(wakePreferenceKey);
      }
    } catch (error) {
      console.warn("Could not save Mira wake-word preference:", error);
    }
  };

  const disableWakeWord = (message) => {
    wakeWordEnabled = false;
    recognitionFailed = true;
    clearTimeout(restartTimer);
    clearTimeout(submitTimer);
    persistWakePreference();
    updateWakeButton();
    status.textContent = message;
  };

  const startRecognition = () => {
    if (!recognition || !wakeWordEnabled || assistantBusy || isSpeaking || isListening || isStarting) return;
    try {
      isStarting = true;
      recognition.start();
    } catch (error) {
      isStarting = false;
      console.error("Could not start Mira wake-word recognition:", error);
      disableWakeWord("Wake-word listening could not start. Check microphone permission and try again.");
    }
  };

  const scheduleRecognitionRestart = () => {
    clearTimeout(restartTimer);
    if (wakeWordEnabled && !assistantBusy && !isSpeaking && !recognitionFailed && !submitTimer) {
      restartTimer = window.setTimeout(startRecognition, 350);
    }
  };

  const stopRecognition = () => {
    clearTimeout(restartTimer);
    clearTimeout(submitTimer);
    if (isListening || isStarting) recognition.stop();
  };

  const speakReply = (text) => {
    if (!("speechSynthesis" in window) || !("SpeechSynthesisUtterance" in window)) {
      status.textContent = "Speech output is not supported by this browser. The reply is shown above.";
      assistantBusy = false;
      scheduleRecognitionRestart();
      return;
    }

    isSpeaking = true;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    const voice = selectFeminineVoice();
    if (voice) {
      utterance.voice = voice;
      utterance.lang = voice.lang;
    }
    utterance.pitch = 1.05;
    utterance.onstart = () => {
      status.textContent = "Speaking the answer…";
    };
    utterance.onend = () => {
      isSpeaking = false;
      assistantBusy = false;
      status.textContent = wakeWordEnabled
        ? "Answer read aloud. Say “Hey Mira” to ask another question."
        : "Answer read aloud. You can type another question.";
      scheduleRecognitionRestart();
    };
    utterance.onerror = (event) => {
      console.error("Admin assistant speech output failed:", event.error);
      isSpeaking = false;
      assistantBusy = false;
      status.textContent = "Speech output failed. The reply is still shown above.";
      scheduleRecognitionRestart();
    };
    window.speechSynthesis.speak(utterance);
  };

  const setOpen = (open) => {
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) requestAnimationFrame(() => input.focus());
  };

  addMessage(
    "assistant",
    "I’m Mira. Enable my wake word and say “Hey Mira” followed by a question about revenue, billing, or freezing availability."
  );
  toggle.addEventListener("click", () => setOpen(panel.hidden));
  close.addEventListener("click", () => {
    setOpen(false);
    toggle.focus();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question || assistantBusy) return;

    assistantBusy = true;
    wakePhraseHeard = false;
    wakeResultIndex = null;
    commandFinalTranscript = "";
    commandInterimTranscript = "";
    let answerToSpeak = null;
    addMessage("user", question);
    input.value = "";
    submit.disabled = true;
    stopRecognition();
    status.textContent = "Checking the admin records...";
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ question }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "The assistant request failed.");
      addMessage("assistant", result.answer);
      answerToSpeak = result.answer;
    } catch (error) {
      console.error("Admin assistant request failed:", error);
      addMessage(
        "error",
        error instanceof Error ? error.message : "The assistant request failed. Please try again."
      );
    } finally {
      submit.disabled = false;
      if (answerToSpeak) {
        status.textContent = "Preparing spoken answer…";
      } else {
        assistantBusy = false;
        status.textContent = wakeWordEnabled
          ? "Say “Hey Mira” when you’re ready to ask another question."
          : "Enable “Hey Mira” to listen while signed in, or type a question.";
        scheduleRecognitionRestart();
      }
      if (!wakeWordEnabled) input.focus();
    }
    if (answerToSpeak) speakReply(answerToSpeak);
  });

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    mic.disabled = true;
    mic.title = "Wake-word recognition is not supported by this browser.";
    mic.querySelector("span").textContent = "Wake word unavailable";
    status.textContent = "Wake-word recognition is not supported by this browser. You can type a question instead.";
  } else {
    recognition = new SpeechRecognition();
    recognition.lang = "en-IN";
    recognition.interimResults = true;
    recognition.continuous = true;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => {
      isStarting = false;
      isListening = true;
      recognitionFailed = false;
      status.textContent = wakePhraseHeard
        ? "Mira heard you. Listening for your question…"
        : "Wake word is on. Say “Hey Mira” followed by your question.";
    };

    recognition.onresult = (event) => {
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        const transcript = result[0].transcript.trim();
        const wakeMatch = transcript.match(/\b(?:hey\s+)?mira\b[\s,!.?;:-]*(.*)/i);
        if (!wakePhraseHeard || (wakeMatch && index === wakeResultIndex)) {
          if (!wakeMatch) continue;
          if (!wakePhraseHeard) wakeResultIndex = index;
          wakePhraseHeard = true;
          const command = wakeMatch[1].trim();
          if (result.isFinal) {
            commandFinalTranscript = command;
            commandInterimTranscript = "";
            wakeResultIndex = null;
          } else {
            commandInterimTranscript = command;
          }
        } else if (result.isFinal) {
          commandFinalTranscript = `${commandFinalTranscript} ${transcript}`.trim();
          commandInterimTranscript = "";
        } else {
          commandInterimTranscript = transcript;
        }
      }
      if (!wakePhraseHeard) return;

      input.value = `${commandFinalTranscript} ${commandInterimTranscript}`.trim();
      input.dispatchEvent(new Event("input", { bubbles: true }));
      status.textContent = commandInterimTranscript
        ? `Mira heard you: ${commandInterimTranscript.trim()}`
        : "Mira heard you. Listening for your question…";
      messages.scrollTop = messages.scrollHeight;

      if (commandFinalTranscript) {
        clearTimeout(submitTimer);
        submitTimer = window.setTimeout(() => {
          if (!assistantBusy && input.value.trim()) form.requestSubmit();
        }, 1200);
      }
    };

    recognition.onerror = (event) => {
      if (recognitionFailed) return;

      const details = event.error === "network"
        ? "Browser speech recognition is unavailable because of a network error. You can type a question instead."
        : event.error === "not-allowed"
          ? "Microphone access was not allowed. You can type a question or enable the microphone in browser settings."
          : `Wake-word listening failed (${event.error}). You can type a question instead.`;
      if (["not-allowed", "service-not-allowed", "audio-capture", "network"].includes(event.error)) {
        disableWakeWord(details);
      } else {
        status.textContent = details;
      }
    };

    recognition.onend = () => {
      isListening = false;
      isStarting = false;
      if (!recognitionFailed) {
        if (wakePhraseHeard && input.value.trim() && !assistantBusy) {
          status.textContent = "Mira heard you. Finishing your question…";
          if (!submitTimer) submitTimer = window.setTimeout(() => form.requestSubmit(), 500);
        } else if (wakeWordEnabled && !assistantBusy && !isSpeaking) {
          status.textContent = "Wake word is on. Say “Hey Mira” followed by your question.";
        }
        scheduleRecognitionRestart();
      }
    };

    mic.addEventListener("click", () => {
      wakeWordEnabled = !wakeWordEnabled;
      recognitionFailed = false;
      persistWakePreference();
      updateWakeButton();
      if (wakeWordEnabled) {
        wakePhraseHeard = false;
        wakeResultIndex = null;
        commandFinalTranscript = "";
        commandInterimTranscript = "";
        input.value = "";
        status.textContent = "Starting continuous wake-word listening…";
        startRecognition();
      } else {
        stopRecognition();
        wakePhraseHeard = false;
        status.textContent = "Wake word off. You can type a question or turn “Hey Mira” back on.";
      }
    });

    try {
      wakeWordEnabled = window.localStorage.getItem(wakePreferenceKey) === "true";
    } catch (error) {
      console.warn("Could not restore Mira wake-word preference:", error);
    }
    updateWakeButton();
    if (wakeWordEnabled) {
      status.textContent = "Starting saved “Hey Mira” wake word…";
      startRecognition();
    }
  }
})();
