'use strict';

const packageDialog = el('packaging');
const coverImage = el('cover-frame');
const coverTime = el('cover-time');
const packageSave = el('save-packaging');
let packageClip = null;
let coverRequest, coverTimer, coverUrl, shownTime;

function packageSuggestions() {
    const title = el('package-title').value.trim();
    const cover = el('cover-text').value.trim();
    const chars = Array.from(title);
    const compact = chars.slice(0, 42).join('');
    el('package-title-preview').textContent = chars.length > 42 ?
        'Compact preview: ' + compact + '…' : chars.length + ' / 100 characters';

    const hints = [];
    if (!title) hints.push('Add a title that names the moment or its payoff.');
    if (chars.length > 60) hints.push('Put the subject and payoff early. Longer titles may be cut off.');
    if ((title.match(/\b[A-Z]{3,}\b/g) || []).length >= 3) hints.push('Use sentence case for easier scanning.');
    const words = text => text.toLowerCase().match(/[\p{L}\p{M}\p{N}]+/gu) || [];
    const titleWords = new Set(words(title));
    const coverWords = words(cover);
    const filler = new Set(['a','an','the','of','to','and','in','is','it','on','for','at','my','you','your']);
    const meaningful = [...new Set(coverWords.filter(word => !filler.has(word)))];
    const repeats = meaningful.filter(word => titleWords.has(word));
    if (repeats.length && repeats.length / meaningful.length >= .6) {
        hints.push('The cover repeats the title. Use the frame or its text to reveal a different detail.');
    }
    if (coverWords.length > 4) hints.push('Check the cover at a small size. A shorter phrase is easier to read.');
    if (!hints.length) hints.push(cover ? 'The pairing passes these basic checks.' : 'Check the frame for a clear subject at this size.');
    el('package-checks').replaceChildren(...hints.map(hint => create('li', hint)));
    packageSave.disabled = !title || shownTime === undefined || !packageClip;
}

async function showCover() {
    const clip = packageClip;
    if (!clip || coverTime.disabled) return;
    coverRequest?.abort();
    const controller = new AbortController();
    coverRequest = controller;
    const seconds = Number(coverTime.value);
    shownTime = undefined;
    el('cover-time-label').textContent = seconds.toFixed(2) + ' s';
    packageSuggestions();
    try {
        const response = await fetch('/review/' + clip.id + '/cover?at=' + seconds, {signal:controller.signal});
        if (!response.ok) throw Error((await response.json()).detail || 'Could not load this frame.');
        const frame = await response.blob();
        if (controller.signal.aborted || packageClip !== clip) return;
        if (coverUrl) URL.revokeObjectURL(coverUrl);
        coverUrl = URL.createObjectURL(frame);
        coverImage.src = coverUrl;
        await coverImage.decode();
        if (controller.signal.aborted || packageClip !== clip) return;
        shownTime = seconds;
        el('packaging-message').textContent = '';
        packageSuggestions();
    } catch (err) {
        if (err.name !== 'AbortError' && packageClip === clip) el('packaging-message').textContent = err.message;
    }
}

async function openPackaging(clip) {
    packageClip = clip;
    coverTime.disabled = true;
    shownTime = undefined;
    coverImage.removeAttribute('src');
    el('package-title').value = clip.title;
    el('cover-text').value = clip.cover_text || '';
    el('cover-time-label').textContent = '';
    el('packaging-message').textContent = 'Loading clip…';
    packageSuggestions();
    packageDialog.showModal();
    try {
        const response = await fetch('/review/' + clip.id + '/packaging');
        if (!response.ok) throw Error((await response.json()).detail || 'Could not load this clip.');
        const {duration} = await response.json();
        if (packageClip !== clip) return;
        const lastFrame = Math.max(0, duration - .08);
        coverTime.max = lastFrame;
        coverTime.value = Math.min(lastFrame, clip.cover_seconds ?? Math.min(.5, duration * .1));
        coverTime.disabled = false;
        await showCover();
    } catch (err) {
        if (packageClip === clip) el('packaging-message').textContent = err.message;
    }
}

coverTime.oninput = () => {
    coverRequest?.abort();
    clearTimeout(coverTimer);
    shownTime = undefined;
    el('cover-time-label').textContent = Number(coverTime.value).toFixed(2) + ' s';
    packageSuggestions();
    coverTimer = setTimeout(showCover, 180);
};
el('package-title').oninput = packageSuggestions;
el('cover-text').oninput = packageSuggestions;
el('close-packaging').onclick = () => packageDialog.close();
packageDialog.onclose = () => {
    packageClip = null;
    coverRequest?.abort();
    clearTimeout(coverTimer);
    if (coverUrl) URL.revokeObjectURL(coverUrl);
    coverUrl = undefined;
    coverImage.removeAttribute('src');
};
el('packaging-form').onsubmit = async event => {
    event.preventDefault();
    if (!packageClip || shownTime === undefined || packageSave.disabled) return;
    const clip = packageClip;
    const payload = {title:el('package-title').value, cover_text:el('cover-text').value, cover_seconds:shownTime};
    packageSave.disabled = true;
    try {
        await post('/review/' + clip.id + '/packaging', payload);
        packageDialog.close();
        el('message').textContent = 'Title and cover reference saved. This Short is ready for approval.';
        await refresh();
    } catch (err) {
        el('packaging-message').textContent = err.message;
        packageSuggestions();
    }
};

const retentionForm = el('retention-form');
const retentionPoint = el('retention-point');
const retentionCanvas = el('retention-chart');
let retention;

function axisLabel(value, unit) {
    if (unit === 'percent') return Number(value.toFixed(2)) + '% of video';
    const minutes = Math.floor(value / 60);
    const seconds = value % 60;
    return minutes ? minutes + ':' + seconds.toFixed(1).padStart(4, '0') : seconds.toFixed(1) + ' s';
}

function drawRetention() {
    const points = retention.points;
    const g = retentionCanvas.getContext('2d');
    const w = retentionCanvas.width, h = retentionCanvas.height;
    const left = 55, top = 15, right = w - 20, bottom = h - 36;
    const minX = points[0][0], maxX = points.at(-1)[0];
    const peak = points.reduce((highest, point) => Math.max(highest, point[1]), 100);
    const maxY = Math.ceil(peak / 25) * 25;
    const x = value => left + (value - minX) / (maxX - minX) * (right - left);
    const y = value => bottom - value / maxY * (bottom - top);
    g.clearRect(0, 0, w, h);
    g.font = '15px system-ui';
    g.lineWidth = 1;
    g.fillStyle = '#cbcad9';
    g.strokeStyle = '#484458';
    g.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
        const value = maxY * i / 4;
        const py = y(value);
        g.beginPath();
        g.moveTo(left, py);
        g.lineTo(right, py);
        g.stroke();
        g.fillText(Number(value.toFixed(1)) + '%', left - 7, py + 5);
    }
    g.textAlign = 'left';
    g.fillText(axisLabel(minX, retention.unit), left, h - 9);
    g.textAlign = 'right';
    g.fillText(axisLabel(maxX, retention.unit), right, h - 9);
    g.beginPath();
    for (let i = 0; i < points.length; i++) {
        const [position, value] = points[i];
        if (i === 0) g.moveTo(x(position), y(value));
        else g.lineTo(x(position), y(value));
    }
    g.strokeStyle = '#c0b2ff';
    g.lineWidth = 3;
    g.stroke();
    const [position, value] = points[Number(retentionPoint.value)];
    g.fillStyle = '#ffe1a6';
    g.beginPath();
    g.arc(x(position), y(value), 6, 0, Math.PI * 2);
    g.fill();
    el('retention-reading').textContent = axisLabel(position, retention.unit) + ' · ' + value.toFixed(1) + '% retention';
}

function showRetention(data) {
    retention = data;
    const {points, opening, drops, unit} = data;
    const openingText = opening ? ' · Opening change at ' + axisLabel(opening.at, unit) + ': ' +
        (opening.change > 0 ? '+' : '') + opening.change.toFixed(1) + ' percentage points' : '';
    el('retention-summary').textContent =
        'First point ' + points[0][1].toFixed(1) + '% · Last point ' + points.at(-1)[1].toFixed(1) + '%' + openingText;
    retentionPoint.max = points.length - 1;
    retentionPoint.value = drops[0]?.index || 0;
    const list = el('retention-drops');
    list.replaceChildren();
    for (const drop of drops) {
        const item = create('li');
        const show = create('button', axisLabel(drop.from, unit) + ' to ' + axisLabel(drop.to, unit) +
            ' · ' + drop.loss.toFixed(1) + ' percentage points down');
        show.type = 'button';
        show.onclick = () => {retentionPoint.value = drop.index; drawRetention();};
        item.append(show);
        list.append(item);
    }
    if (!drops.length) list.append(create('li', 'No drops between recorded points.'));
    el('retention-results').hidden = false;
    drawRetention();
}

retentionPoint.oninput = drawRetention;
retentionForm.onsubmit = async event => {
    event.preventDefault();
    const data = new FormData(retentionForm);
    const file = data.get('file');
    const message = el('retention-message');
    if (!file?.size || file.size > 1024 * 1024) {
        message.textContent = 'Choose an audience-retention CSV under 1 MB.';
        return;
    }
    if (!data.get('duration')) data.delete('duration');
    const button = retentionForm.querySelector('button');
    button.disabled = true;
    el('retention-results').hidden = true;
    message.textContent = 'Analyzing locally…';
    try {
        const response = await fetch('/retention/analyze', {method:'POST', headers:{'X-Workflow-Token':token}, body:data});
        const result = await response.json();
        if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'Check the CSV and video length.');
        showRetention(result);
        message.textContent = result.points.length + ' retention points analyzed.';
    } catch (err) {
        message.textContent = err.message;
    } finally {
        button.disabled = false;
    }
};
