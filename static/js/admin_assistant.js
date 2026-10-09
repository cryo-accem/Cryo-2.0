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
  const transcriptionUrl = form.dataset.transcriptionUrl;
  let mediaRecorder = null;
  let recordingStream = null;
  let recordedChunks = [];
  let recordingTimer = null;
  let isRecording = false;
  let discardRecording = false;
  let assistantBusy = false;
  let isSpeaking = false;
  let currentAudio = null;
  let currentAudioUrl = null;

  const addMessage = (kind, text) => {
    const message = document.createElement("article");
    message.className = `admin-assistant__message admin-assistant__message--${kind}`;
    const body = document.createElement("p");
    body.textContent = text;
    message.append(body);
    messages.append(message);
    messages.scrollTop = messages.scrollHeight;
  };

  const updateMicButton = () => {
    mic.classList.toggle("is-listening", isRecording);
    mic.setAttribute("aria-pressed", String(isRecording));
    mic.setAttribute("aria-label", isRecording ? "Stop recording and transcribe" : "Start voice recording");
    mic.querySelector("span").textContent = isRecording ? "Stop & transcribe" : "Start recording";
  };

  const stopRecordingTracks = () => {
    recordingStream?.getTracks().forEach((track) => track.stop());
    recordingStream = null;
  };

  const releaseSpokenAudio = () => {
    if (currentAudio) {
      currentAudio.pause();
      currentAudio.removeAttribute("src");
      currentAudio.load();
      currentAudio = null;
    }
    if (currentAudioUrl) {
      URL.revokeObjectURL(currentAudioUrl);
      currentAudioUrl = null;
    }
    isSpeaking = false;
    assistantBusy = false;
    mic.disabled = false;
  };

  const finishSpokenAudio = (message) => {
    releaseSpokenAudio();
    assistantBusy = false;
    status.textContent = message;
  };

  const showAudioFallback = (messageText) => {
    isSpeaking = false;
    assistantBusy = false;
    mic.disabled = false;
    const message = document.createElement("article");
    message.className = "admin-assistant__message admin-assistant__message--assistant";
    const instruction = document.createElement("p");
    instruction.textContent = messageText;
    const download = document.createElement("a");
    download.href = currentAudioUrl;
    download.download = "mira-spoken-answer.wav";
    download.textContent = "Download Mira’s spoken answer";
    message.append(instruction, download);
    messages.append(message);
    messages.scrollTop = messages.scrollHeight;
    status.textContent = messageText;
  };

  const transcribeRecording = async (recording) => {
    if (!recording.size) {
      status.textContent = "The recording was empty. Try recording again or type your question.";
      return;
    }

    const extension = recording.type.includes("mp4") ? "m4a"
      : recording.type.includes("ogg") ? "ogg"
        : recording.type.includes("wav") ? "wav" : "webm";
    const audio = new FormData();
    audio.append("audio", recording, `mira-question.${extension}`);
    mic.disabled = true;
    status.textContent = "Transcribing your recording…";
    try {
      const response = await fetch(transcriptionUrl, {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
        body: audio,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Voice transcription failed.");
      input.value = result.transcript;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      status.textContent = "Transcript ready. Checking the admin records…";
      mic.disabled = false;
      form.requestSubmit();
    } catch (error) {
      console.error("Admin assistant transcription failed:", error);
      status.textContent = error instanceof Error
        ? error.message
        : "Voice transcription failed. You can type your question instead.";
      mic.disabled = false;
    }
  };

  const stopRecording = () => {
    clearTimeout(recordingTimer);
    if (!mediaRecorder || mediaRecorder.state !== "recording") return;
    status.textContent = "Sending your recording for transcription…";
    mediaRecorder.stop();
  };

  const cancelRecording = () => {
    clearTimeout(recordingTimer);
    if (mediaRecorder?.state === "recording") {
      discardRecording = true;
      mediaRecorder.stop();
    } else {
      stopRecordingTracks();
    }
    isRecording = false;
    updateMicButton();
  };

  const startRecording = async () => {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      status.textContent = "Voice recording is not supported by this browser. You can type your question instead.";
      return;
    }

    mic.disabled = true;
    status.textContent = "Requesting microphone access…";
    try {
      recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = [
        "audio/webm;codecs=opus",
        "audio/mp4",
        "audio/ogg;codecs=opus",
      ].find((candidate) => MediaRecorder.isTypeSupported(candidate));
      mediaRecorder = mimeType
        ? new MediaRecorder(recordingStream, { mimeType })
        : new MediaRecorder(recordingStream);
      recordedChunks = [];
      mediaRecorder.addEventListener("dataavailable", (event) => {
        if (event.data.size) recordedChunks.push(event.data);
      });
      mediaRecorder.addEventListener("stop", () => {
        const mimeType = mediaRecorder.mimeType;
        const recording = new Blob(recordedChunks, { type: mimeType });
        mediaRecorder = null;
        recordedChunks = [];
        isRecording = false;
        updateMicButton();
        stopRecordingTracks();
        if (discardRecording) {
          discardRecording = false;
          status.textContent = "Recording discarded.";
          return;
        }
        transcribeRecording(recording);
      }, { once: true });
      mediaRecorder.start();
      isRecording = true;
      updateMicButton();
      mic.disabled = false;
      status.textContent = "Recording… Ask your question, then tap “Stop & transcribe”.";
      recordingTimer = window.setTimeout(stopRecording, 30000);
    } catch (error) {
      console.error("Could not start Mira voice recording:", error);
      stopRecordingTracks();
      mediaRecorder = null;
      isRecording = false;
      updateMicButton();
      mic.disabled = false;
      status.textContent = error instanceof Error && error.name === "NotAllowedError"
        ? "Microphone access was denied. Allow microphone access in browser settings or type your question."
        : "Recording could not start. Check microphone access or type your question.";
    }
  };

  const speakReply = (text) => {
    releaseSpokenAudio();
    isSpeaking = true;
    mic.disabled = true;
    status.textContent = "Preparing Mira’s feminine spoken answer…";
    fetch(assistant.dataset.speechUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify({ text }),
    })
      .then(async (response) => {
        if (!response.ok) {
          const result = await response.json();
          throw new Error(result.error || "Spoken answer generation failed.");
        }
        return response.blob();
      })
      .then((audioBlob) => {
        currentAudioUrl = URL.createObjectURL(audioBlob);
        currentAudio = new Audio(currentAudioUrl);
        currentAudio.addEventListener("ended", () => {
          finishSpokenAudio("Answer read aloud in Mira’s feminine voice. You can ask another question.");
        }, { once: true });
        currentAudio.addEventListener("play", () => {
          isSpeaking = true;
          assistantBusy = true;
          mic.disabled = true;
          status.textContent = "Speaking the answer in Mira’s feminine voice…";
        });
        currentAudio.addEventListener("error", () => {
          showAudioFallback("This browser couldn’t play the audio. Download Mira’s spoken answer.");
        }, { once: true });
        status.textContent = "Speaking the answer in Mira’s feminine voice…";
        return currentAudio.play().catch((error) => {
          if (error.name !== "NotAllowedError") throw error;
          currentAudio.controls = true;
          currentAudio.setAttribute("aria-label", "Play Mira’s feminine spoken answer");
          const message = document.createElement("article");
          message.className = "admin-assistant__message admin-assistant__message--assistant";
          const instruction = document.createElement("p");
          instruction.textContent = "Tap to hear Mira’s feminine spoken answer.";
          const download = document.createElement("a");
          download.href = currentAudioUrl;
          download.download = "mira-spoken-answer.wav";
          download.textContent = "Download Mira’s spoken answer";
          message.append(instruction, currentAudio);
          message.append(download);
          messages.append(message);
          messages.scrollTop = messages.scrollHeight;
          isSpeaking = false;
          assistantBusy = false;
          mic.disabled = false;
          status.textContent = "Tap the audio player to hear Mira’s feminine voice.";
        });
      })
      .catch((error) => {
        console.error("Admin assistant spoken answer failed:", error);
        finishSpokenAudio(
          error instanceof Error
            ? error.message
            : "Spoken answer generation failed. The answer is still shown above."
        );
      });
  };

  const setOpen = (open) => {
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) requestAnimationFrame(() => input.focus());
  };

  addMessage(
    "assistant",
    "I’m Mira. Tap the microphone, ask a question about revenue, billing, or freezing availability, then tap again to transcribe and send it."
  );
  toggle.addEventListener("click", () => {
    const open = panel.hidden;
    if (!open) {
      cancelRecording();
      releaseSpokenAudio();
    }
    setOpen(open);
  });
  close.addEventListener("click", () => {
    cancelRecording();
    releaseSpokenAudio();
    setOpen(false);
    toggle.focus();
  });
  window.addEventListener("pagehide", () => {
    cancelRecording();
    releaseSpokenAudio();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question || assistantBusy) return;

    cancelRecording();
    assistantBusy = true;
    let answerToSpeak = null;
    addMessage("user", question);
    input.value = "";
    submit.disabled = true;
    mic.disabled = true;
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
      mic.disabled = false;
      if (answerToSpeak) {
        status.textContent = "Preparing spoken answer…";
      } else {
        assistantBusy = false;
        status.textContent = "Tap the microphone to record a question, or type one below.";
      }
      input.focus();
    }
    if (answerToSpeak) speakReply(answerToSpeak);
  });

  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    mic.disabled = true;
    mic.title = "Voice recording is not supported by this browser.";
    status.textContent = "Voice recording is not supported by this browser. You can type a question instead.";
  } else {
    updateMicButton();
    mic.addEventListener("click", () => {
      if (assistantBusy || isSpeaking) return;
      if (isRecording) stopRecording();
      else startRecording();
    });
  }
})();
