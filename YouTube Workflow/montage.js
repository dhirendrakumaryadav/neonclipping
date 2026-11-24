'use strict';

const montageParts = [];
const montageLibrary = new Map();
const montageSource = el('montage-source');
let montageMusic = null, montageResult = null, montageBusy = false, montageUploading = false;
let montagePreviewEnd = null;
const montageMessage = text => { el('montage-status').textContent = text; };
const montageSeconds = () => montageParts.reduce((total, part) => total + (part.end - part.start), 0);

function montageButtons() {
    const ready = !montageBusy && !montageUploading;
    el('montage-add').disabled = !ready || !el('montage-library').value || montageParts.length >= 12;
    el('montage-file').disabled = !ready || montageParts.length >= 12;
    el('montage-music').disabled = !ready;
    el('montage-clear-music').disabled = !ready || !montageMusic;
    el('montage-export').disabled = !ready || montageParts.length < 2 || montageSeconds() > 180;
    el('montage-review').disabled = !ready || !montageResult;
}

async function refreshMontageLibrary() {
    try {
        const response = await fetch('/montage/library');
        if (!response.ok) throw Error((await response.json()).detail);
        const entries = await response.json();
        const select = el('montage-library');
        const selected = select.value;
        select.replaceChildren(new Option('Choose a saved clip', ''));
        montageLibrary.clear();
        for (const item of entries) {
            montageLibrary.set(item.id, item);
            select.add(new Option(item.title, item.id));
        }
        if (montageLibrary.has(selected)) select.value = selected;
        montageButtons();
    } catch (err) {
        montageMessage(err.message || 'Could not load My Clips. You can still add video files.');
    }
}

function addMontagePart(item) {
    if (!item || montageParts.length >= 12) return;
    montageParts.push({id:item.id, title:item.title, duration:item.duration, start:0, end:Math.min(item.duration, 60), layout:'fit'});
}

function drawMontage() {
    const host = el('montage-parts');
    host.replaceChildren();
    for (const [index, part] of montageParts.entries()) {
        const item = create('li');
        item.className = 'item';
        item.append(create('strong', part.title));
        const controls = create('div');
        controls.className = 'publish-controls';
        for (const key of ['start','end']) {
            const label = create('label', key === 'start' ? 'Start (s)' : 'End (s)');
            const input = create('input');
            input.type = 'number';
            input.step = 'any';
            input.min = '0';
            input.max = part.duration;
            input.value = part[key];
            input.setAttribute('aria-label', (key === 'start' ? 'Start: ' : 'End: ') + part.title);
            input.onchange = () => {
                const value = Number(input.value);
                const start = key === 'start' ? value : part.start;
                const end = key === 'end' ? value : part.end;
                if (input.value === '' || !Number.isFinite(value) || start < 0 || end > part.duration || end - start < .25) {
                    input.value = part[key];
                    montageMessage('Each trim must be within its source and at least a quarter-second long.');
                    return;
                }
                part[key] = value;
                montageMessage(montageSeconds().toFixed(2) + ' seconds in montage.');
                montageButtons();
            };
            label.append(input);
            controls.append(label);
        }
        const label = create('label', 'Framing');
        const choice = create('select');
        choice.add(new Option('Fit entire frame', 'fit'));
        choice.add(new Option('Fill vertical frame', 'fill'));
        choice.value = part.layout;
        choice.onchange = () => { part.layout = choice.value; };
        label.append(choice);
        controls.append(label);
        const preview = create('button', 'Preview trim');
        preview.type = 'button';
        preview.onclick = () => {
            montageSource.hidden = false;
            montagePreviewEnd = part.end;
            const url = '/montage/source/' + part.id;
            const seek = () => { montageSource.currentTime = part.start; };
            if (montageSource.getAttribute('src') === url && montageSource.readyState >= 1) seek();
            else {
                montageSource.onloadedmetadata = seek;
                montageSource.src = url;
            }
            montageSource.scrollIntoView({block:'nearest'});
        };
        controls.append(preview);
        for (const [label,shift] of [['Move up',-1],['Move down',1]]) {
            const button = create('button', label);
            button.type = 'button';
            button.disabled = index + shift < 0 || index + shift >= montageParts.length;
            button.onclick = () => {
                const other = index + shift;
                [montageParts[index], montageParts[other]] = [montageParts[other], montageParts[index]];
                drawMontage();
            };
            controls.append(button);
        }
        const remove = create('button', 'Remove from montage');
        remove.type = 'button';
        remove.onclick = () => {montageParts.splice(index, 1); drawMontage();};
        controls.append(remove);
        item.append(controls);
        host.append(item);
    }
    montageMessage(montageParts.length ? montageSeconds().toFixed(2) + ' seconds in montage.' : '');
    montageButtons();
}

montageSource.ontimeupdate = () => {
    if (montagePreviewEnd !== null && montageSource.currentTime >= montagePreviewEnd && !montageSource.paused) {
        montageSource.pause();
    }
};
el('montage-library').onchange = montageButtons;
el('montage-add').onclick = () => {addMontagePart(montageLibrary.get(el('montage-library').value)); drawMontage();};
el('montage-volume').oninput = () => {el('montage-level').textContent = el('montage-volume').value + '%';};
el('montage-clear-music').onclick = () => {
    montageMusic = null;
    el('montage-music').value = '';
    montageMessage('Music removed.');
    montageButtons();
};

async function sendMontageAsset(file, kind) {
    const data = new FormData();
    data.append('file', file);
    const response = await fetch('/montage/asset/' + kind, {method:'POST',headers:{'X-Workflow-Token':token},body:data});
    const result = await response.json();
    if (!response.ok) throw Error(result.detail || 'Could not read this file.');
    return result;
}

el('montage-file').onchange = async event => {
    const input = event.currentTarget;
    const files = [...input.files].slice(0, 12 - montageParts.length);
    montageUploading = true;
    montageButtons();
    let firstError = '';
    try {
        for (const file of files) {
            montageMessage('Loading ' + file.name + '…');
            try {addMontagePart(await sendMontageAsset(file, 'video'));}
            catch (err) { firstError ||= err.message; }
        }
        drawMontage();
        if (firstError) montageMessage(firstError);
    } finally {
        input.value = '';
        montageUploading = false;
        montageButtons();
    }
};

el('montage-music').onchange = async event => {
    const input = event.currentTarget;
    const file = input.files[0];
    if (!file) return;
    montageUploading = true;
    montageButtons();
    montageMessage('Loading music…');
    try {
        montageMusic = await sendMontageAsset(file, 'audio');
        montageMessage('Music ready: ' + montageMusic.title);
    } catch (err) {
        montageMessage(err.message);
        input.value = '';
    } finally {
        montageUploading = false;
        montageButtons();
    }
};

el('montage-form').onsubmit = async event => {
    event.preventDefault();
    if (el('montage-export').disabled) return;
    montageBusy = true;
    montageResult = null;
    el('montage-result').hidden = true;
    montageButtons();
    try {
        const {job} = await post('/montage/render', {
            title:el('montage-title').value,
            caption:el('montage-caption').value,
            parts:montageParts.map(({id,start,end,layout}) => ({id,start,end,layout})),
            music:montageMusic?.id,
            music_volume:Number(el('montage-volume').value)/100,
        });
        while (true) {
            const response = await fetch('/montage/render/' + job);
            const state = await response.json();
            if (!response.ok || state.status === 'error') throw Error(state.detail || 'Could not finish the export.');
            if (state.status === 'done') {
                montageResult = state.file;
                el('montage-preview').src = '/montage/source/' + state.file;
                el('montage-result').hidden = false;
                montageMessage('Combined Short exported. Preview it before sending it for review.');
                refreshMontageLibrary();
                break;
            }
            montageMessage(state.detail);
            await new Promise(resolve => setTimeout(resolve, 1200));
        }
    } catch (err) {
        montageMessage(err.message);
    } finally {
        montageBusy = false;
        montageButtons();
    }
};

el('montage-review').onclick = async () => {
    if (!montageResult || montageBusy) return;
    montageBusy = true;
    montageButtons();
    try {
        const result = await post('/review/library/' + montageResult);
        montageMessage(result.added ? 'Added to Review clips. Approval is still required.' : 'This export is already in the queue.');
        await refresh();
    } catch (err) {
        montageMessage(err.message);
    } finally {
        montageBusy = false;
        montageButtons();
    }
};
refreshMontageLibrary();
