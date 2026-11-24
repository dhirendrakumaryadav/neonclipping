'use strict';

const takeDialog = el('commentary-dialog');
const takeSource = el('commentary-source');
const takePreview = el('commentary-preview');
const takeOffset = el('commentary-offset');
const takeFile = el('commentary-file');
const payoffOffset = el('payoff-offset');
const lineDrafts = new Map();
const takeMessage = text => {el('commentary-status').textContent = text;};
let talk = null;

function takeControls() {
    const recording = Boolean(talk?.recorder);
    const busy = Boolean(talk?.busy);
    const hasPreview = Boolean(talk?.take);
    const ready = Boolean(talk && !recording && !busy && takeSource.readyState >= 1 && takeSource.duration > .25 && !talk.linesLoading);
    el('record-take').disabled = !ready;
    for (const slot of ['opening', 'payoff']) {
        const button = el('record-' + slot);
        button.disabled = !ready || (slot === 'payoff' && !validPayoff());
        button.textContent = talk?.split?.[slot] ? 'Retake ' + slot : 'Record ' + slot;
    }
    el('stop-take').disabled = !recording;
    const haveAudio = talk?.split ? talk.split.opening || talk.split.payoff : talk?.blob;
    el('render-take').disabled = !haveAudio || recording || busy || Boolean(talk?.split?.payoff && !validPayoff());
    el('apply-commentary').disabled = !hasPreview || talk.take === talk.clip.commentary_take || recording || busy;
    el('restore-commentary').hidden = !talk?.clip.commentary_take;
    el('restore-commentary').disabled = recording || busy;
    el('draft-commentary').disabled = !talk || recording || busy || talk.drafting;
    el('script-commentary').disabled = !talk || recording || busy || talk.drafting;
    takeOffset.disabled = recording || busy;
    payoffOffset.disabled = recording || busy;
    takeFile.disabled = recording || busy;
    takeSource.controls = !recording;
    if (ready && talk.focusSlot) {
        const button = el('record-' + talk.focusSlot);
        if (!button.disabled) button.focus();
        talk.focusSlot = null;
    }
}

function showTake(id, url = '') {
    talk.take = id;
    const result = el('commentary-result');
    result.hidden = !id;
    if (id) takePreview.src = url || '/review/' + talk.clip.id + '/commentary/takes/' + id;
    else {
        takePreview.pause();
        takePreview.removeAttribute('src');
        takePreview.load();
    }
    takeControls();
}

function validPayoff() {
    const value = Number(payoffOffset.value);
    return payoffOffset.value !== '' && Number.isFinite(value) && value >= .25 &&
        value < takeSource.duration - .25;
}

function initializePayoff(session) {
    const duration = takeSource.duration;
    if (!session?.split || !Number.isFinite(duration) || duration <= .5) return;
    payoffOffset.max = duration - .251;
    if (session.split.offset !== undefined) {
        payoffOffset.value = session.split.offset;
        return;
    }
    const suggested = session.suggestedPayoff;
    if (suggested != null && suggested >= .25 && suggested < duration - .25) {
        payoffOffset.value = suggested;
    } else {
        payoffOffset.value = Math.max(.25, Math.min(duration - .251, Math.round(duration * 6) / 10));
    }
}

async function loadVoiceLines(session) {
    try {
        const response = await fetch('/review/' + session.clip.id + '/commentary/lines');
        if (!response.ok) throw Error();
        const lines = await response.json();
        if (talk !== session) return;
        el('opening-words').textContent = lines.opening || '';
        el('payoff-words').textContent = lines.payoff || '';
        session.suggestedPayoff = lines.payoff_at;
        if (!session.recorder && !session.busy) initializePayoff(session);
    } catch {
        if (talk === session) takeMessage('Could not load the suggested lines. Set the payoff time to record your own.');
    } finally {
        session.linesLoading = false;
        if (talk === session) takeControls();
    }
}

function openCommentary(clip, mode = 'full') {
    const split = mode !== 'full';
    if (split && !lineDrafts.has(clip.id)) lineDrafts.set(clip.id, {});
    talk = {clip, take:null, blob:null, recorder:null, drafting:false, busy:false,
        split: split ? lineDrafts.get(clip.id) : null, focusSlot: split ? mode : null, linesLoading: split};
    el('split-commentary').hidden = !split;
    for (const id of ['full-offset', 'full-script-fields', 'record-take', 'full-recording-file']) {
        el(id).hidden = split;
    }
    el('render-take').textContent = split ? 'Preview recorded lines' : 'Enhance and preview';
    takeSource.src = '/review/' + clip.id + '/commentary/original';
    takeOffset.value = 0;
    takeOffset.removeAttribute('max');
    payoffOffset.value = '';
    payoffOffset.removeAttribute('max');
    el('opening-words').textContent = '';
    el('payoff-words').textContent = '';
    el('commentary-angle').value = '';
    el('commentary-draft').value = '';
    el('commentary-language').value = 'auto';
    takeFile.value = '';
    el('commentary-recording').textContent = '';
    takeMessage('');
    showTake(talk.split?.dirty ? null : clip.commentary_latest || clip.commentary_take);
    takeDialog.showModal();
    if (split) loadVoiceLines(talk);
}

function stopTake() {
    const active = talk?.recorder;
    if (active && active.state !== 'inactive') active.stop();
}

takeSource.onloadedmetadata = () => {
    takeOffset.max = Math.max(0, takeSource.duration - .3);
    initializePayoff(talk);
    takeControls();
};
takeSource.ontimeupdate = () => {
    if (talk?.stopAt != null && takeSource.currentTime >= talk.stopAt) stopTake();
};
payoffOffset.oninput = () => {
    if (!talk?.split) return;
    talk.split.offset = payoffOffset.value;
    if (talk.split.payoff) {
        talk.split.dirty = true;
        showTake(null);
    } else takeControls();
};
takeSource.onended = stopTake;
takeSource.onpause = stopTake;
el('stop-take').onclick = stopTake;
el('close-commentary').onclick = () => takeDialog.close();
takeDialog.onclose = () => {
    const session = talk;
    stopTake();
    talk = null;
    session?.stream?.getTracks().forEach(track => track.stop());
    for (const player of [takeSource,takePreview]) {
        player.pause();
        player.muted = false;
        player.removeAttribute('src');
        player.load();
    }
};
takeFile.onchange = () => {
    if (!talk) return;
    talk.blob = takeFile.files[0] || null;
    el('commentary-recording').textContent = talk.blob?.name || '';
    takeControls();
};

el('draft-commentary').onclick = async () => {
    const session = talk;
    if (!session || session.drafting) return;
    session.drafting = true;
    takeControls();
    takeMessage('Writing a draft from the clip details…');
    try {
        const result = await post('/review/' + session.clip.id + '/commentary/draft', {
            angle: el('commentary-angle').value,
            offset: Number(takeOffset.value),
        });
        if (talk !== session) return;
        el('commentary-draft').value = result.draft;
        takeMessage(result.source === 'template' ? 'Draft starter ready. Add a specific detail from the clip before reading.' :
            'Draft ready. Read it through and change anything that doesn’t sound like you.');
    } catch (err) {
        if (talk === session) takeMessage(err.message);
    } finally {
        session.drafting = false;
        if (talk === session) takeControls();
    }
};

el('script-commentary').onclick = async () => {
    const session = talk;
    if (!session || session.drafting) return;
    session.drafting = true;
    takeControls();
    takeMessage('Writing a full script for this clip… (a few seconds)');
    try {
        const result = await post('/review/' + session.clip.id + '/commentary/script', {
            style: el('commentary-style').value,
            angle: el('commentary-angle').value,
            offset: Number(takeOffset.value),
        });
        if (talk !== session) return;
        const stamp = at => Math.floor(at / 60) + ':' + String(Math.floor(at % 60)).padStart(2, '0');
        el('commentary-draft').value = result.lines.map(line => `[${stamp(line.at)}] ${line.line}`).join('\n');
        takeMessage(result.source === 'template'
            ? 'The local model is not available, so here is a starter. Fill in the [brackets] with your own jokes.'
            : 'Script ready. Times count from where your voice starts. Change any line that doesn’t sound like you, then record.');
    } catch (err) {
        if (talk === session) takeMessage(err.message);
    } finally {
        session.drafting = false;
        if (talk === session) takeControls();
    }
};

async function recordFor(slot) {
    const session = talk;
    if (!session || session.busy || session.recorder) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        takeMessage('Microphone recording needs a browser that supports it. You can also choose an audio file.');
        return;
    }
    const start = slot === 'opening' ? 0 : Number(slot === 'payoff' ? payoffOffset.value : takeOffset.value);
    if (slot === 'payoff' ? !validPayoff() : !Number.isFinite(start) || start < 0 ||
        start > Number(takeOffset.max)) {
        takeMessage('Choose a start time within the clip.');
        return;
    }
    session.stopAt = slot === 'opening' && validPayoff() ? Number(payoffOffset.value) : null;
    session.busy = true;
    takeControls();
    try {
        takeSource.pause();
        takePreview.pause();
        session.stream = await navigator.mediaDevices.getUserMedia({
            audio: {echoCancellation:true, noiseSuppression:true, autoGainControl:false}, video:false,
        });
        if (session !== talk) { session.stream.getTracks().forEach(track => track.stop()); return; }
        takeSource.currentTime = start;
        takeSource.playbackRate = 1;
        takeSource.muted = true;
        await takeSource.play();
        if (session !== talk) { session.stream.getTracks().forEach(track => track.stop()); return; }
        const recorder = new MediaRecorder(session.stream);
        const chunks = [];
        const actualStart = takeSource.currentTime;
        session.recorder = recorder;

        recorder.ondataavailable = event => {if (event.data.size) chunks.push(event.data);};
        recorder.onstop = () => {
            session.stream.getTracks().forEach(track => track.stop());
            session.recorder = null;
            if (session !== talk) return;
            takeSource.pause();
            takeSource.muted = false;
            const blob = new Blob(chunks, {type:recorder.mimeType || 'audio/webm'});
            if (blob.size && session.split) {
                session.split[slot] = blob;
                if (slot === 'payoff') session.split.offset = payoffOffset.value;
                session.split.dirty = true;
                showTake(null);
                takeMessage(slot === 'opening' ? 'Opening recorded. Record the payoff or preview your opening.' :
                    'Payoff recorded. Preview your recorded lines together.');
            } else if (blob.size) {
                session.blob = blob;
                takeFile.value = '';
                takeOffset.value = Math.round(actualStart * 1000) / 1000;
                el('commentary-recording').textContent = 'Microphone take recorded.';
                showTake(null);
                takeMessage('Take ready. Enhance and preview it before applying.');
            } else {
                takeMessage('The microphone did not capture audio. Try again.');
            }
            takeControls();
        };
        recorder.onerror = () => {
            if (session === talk) takeMessage('Recording stopped because the microphone had a problem.');
        };
        recorder.start();
        takeMessage(slot === 'full' ? 'Recording. Read your script while the muted clip plays.' :
            'Recording the ' + slot + '. Read the line, then stop recording.');
    } catch (err) {
        session.recorder = null;
        session.stream?.getTracks().forEach(track => track.stop());
        if (session === talk) {
            takeSource.pause();
            takeSource.muted = false;
            takeMessage(err.name === 'NotAllowedError' ? 'Allow microphone access in this browser, or choose a recording.' :
                'Could not start recording. ' + err.message);
        }
    } finally {
        session.busy = false;
        if (session === talk) takeControls();
    }
}
el('record-take').onclick = () => recordFor('full');
el('record-opening').onclick = () => recordFor('opening');
el('record-payoff').onclick = () => recordFor('payoff');

el('render-take').onclick = async () => {
    const session = talk;
    if (!session || session.busy || session.recorder) return;
    const data = new FormData();
    if (session.split) {
        if (!session.split.opening && !session.split.payoff) return;
        if (session.split.payoff && !validPayoff()) {
            takeMessage('Choose a payoff time within the clip.');
            return;
        }
        for (const slot of ['opening', 'payoff']) {
            if (session.split[slot]) data.set(slot, session.split[slot], slot + '-take');
        }
        if (session.split.payoff) data.set('payoff_offset', payoffOffset.value);
    } else {
        if (!session.blob) return;
        const offset = Number(takeOffset.value);
        if (!Number.isFinite(offset) || offset < 0 || offset > Number(takeOffset.max)) {
            takeMessage('Choose a start time within the clip.');
            return;
        }
        data.set('recording', session.blob, session.blob.name || 'microphone-take');
        data.set('offset', offset);
    }
    data.set('language', el('commentary-language').value);
    session.busy = true;
    takeControls();
    takeMessage('Cleaning up your voice, making captions, and rendering the preview locally…');
    try {
        const response = await fetch('/review/' + session.clip.id + '/commentary/render', {
            method:'POST', headers:{'X-Workflow-Token':token}, body:data,
        });
        const result = await response.json();
        if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'Could not render this take.');
        if (session !== talk) return;
        if (session.split) session.split.dirty = false;
        showTake(result.take, result.preview);
        takeMessage('Play the enhanced preview. Choose Use this preview when it sounds right.');
    } catch (err) {
        if (session === talk) takeMessage(err.message);
    } finally {
        session.busy = false;
        if (session === talk) takeControls();
    }
};

async function useTake(take) {
    const session = talk;
    if (!session || session.busy || session.recorder) return;
    session.busy = true;
    takeControls();
    try {
        await post('/review/' + session.clip.id + '/commentary/use', {take});
        if (session === talk) takeDialog.close();
        el('message').textContent = take ? 'Commentary applied. Preview the Short and complete Title + Thumbnail Check before approving.' :
            'Original clip restored. You can preview it before approving.';
        await refresh();
    } catch (err) {
        if (session === talk) takeMessage(err.message);
    } finally {
        session.busy = false;
        if (session === talk) takeControls();
    }
}
el('apply-commentary').onclick = () => useTake(talk?.take);
el('restore-commentary').onclick = () => useTake(null);
