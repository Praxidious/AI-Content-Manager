const $ = id => document.getElementById(id);
// Support an already-running trainer whose template still has the old note button.
if(!$('submit-feedback')&&$('save-note')){
  const button=$('save-note');button.id='submit-feedback';button.textContent='Submit feedback';
  const status=document.createElement('p');status.id='submit-status';status.setAttribute('role','status');
  status.setAttribute('aria-live','polite');button.after(status);
}
const ratings = ['s_tier','good','maybe','bad','never_again'];
const selectedSource = new URLSearchParams(window.location.search).get('source');
const clipsUrl = selectedSource === null ? '/api/clips' : `/api/clips?${new URLSearchParams({source:selectedSource})}`;
const scopeLabel = document.createElement('p');
scopeLabel.className = 'muted';
scopeLabel.textContent = selectedSource === null ? 'Showing clips from all VODs.' : 'Showing clips from the selected VOD only.';
$('error').before(scopeLabel);
let clips = [], index = 0, saving = Promise.resolve();
const token = document.querySelector('meta[name="review-token"]').content;
async function api(url, data) {
  const response = await fetch(url, data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':token},body:JSON.stringify(data)});
  if (!response.ok) { let message = `Request failed (${response.status})`; try {message = (await response.json()).error || message;} catch {} throw Error(message); }
  return response.json();
}
function error(e) {$('error').textContent = e.message;}
function time(seconds) {return new Date(Math.max(0,seconds)*1000).toISOString().slice(11,19);}
function text(tag, value) {const element=document.createElement(tag);element.textContent=value;return element;}
async function summary() {
  const s=await api('/api/summary'); $('total').textContent=s.total_reviewed;
  $('distribution').replaceChildren(...ratings.map(r=>{const el=text('span',r.replaceAll('_',' ').toUpperCase());el.prepend(text('b',s.distribution[r]||0));return el;}));
  for (const [id,key] of [['positive-summary','positive_reasons'],['negative-summary','negative_reasons']]) $(id).textContent=s[key].map(([r,n])=>`${r} (${n})`).join(' · ')||'No reasons yet.';
}
function render() {
  const c=clips[index]; $('editor').hidden=!c;$('empty').hidden=!!c;
  $('position').textContent=c?`${index+1} / ${clips.length}`:'0 candidates';
  $('previous').disabled=!c||index===0;$('next').disabled=!c||index===clips.length-1;
  const video=$('video');video.pause();video.removeAttribute('src');video.load();video.style.display='none';
  $('preview-placeholder').hidden=false;$('preview').disabled=false;$('preview-status').textContent='';
  $('submit-status').textContent='Choose a rating, then submit to confirm your feedback is saved.';
  if(!c)return;
  c.savedPayload??=JSON.stringify({rating:c.rating,reasons:c.reasons,note:c.note||''});
  const m=c.metadata;$('source').textContent=c.source.filename||'Source unavailable';$('title').textContent=m.title||m.description||'Candidate moment';
  $('tier').textContent=m.tier||'Unscored';$('range').textContent=`${time(c.start)} → ${time(c.end)} · ${(c.end-c.start).toFixed(1)} seconds`;
  const score=(v)=>typeof v==='number'?v.toFixed(2):'—';
  $('scores').replaceChildren(...[['Generic',m.generic_quality_score??m.entertainment_score],['Wes preference',m.personal_preference_score??0],['Audience adjustment',m.audience_rank_adjustment??0],['Final rank',m.final_rank_score??m.entertainment_score],['Clipability',m.clipability_score]].map(([k,v])=>text('span',`${k} ${score(v)}`)));
  $('preference').textContent=m.preference_reason||'Personal preference has not been assessed.';
  const audience=m.audience_prediction;
  if($('audience'))$('audience').textContent=audience?`Audience: ${audience.platform} / ${audience.account} · ${audience.history_n} independent clips, ${audience.similar_n} similar · ${audience.confidence} confidence. ${audience.reason}`:'Audience prediction will be assessed on the next analysis run. Import posted results in Performance / Analytics.';
  $('tags').replaceChildren(...(m.content_tags||[]).map(t=>text('span',t)));
  $('detected').textContent=[...new Set([m.why_detected,m.tier_reason,m.reason].filter(Boolean))].join(' ');
  $('transcript').replaceChildren(...(m.transcript_excerpt||[]).map(line=>{const p=text('p',line.text);p.prepend(text('time',time(line.start)));return p;}));
  if(!$('transcript').children.length)$('transcript').textContent=m.exact_transcript_evidence||'No transcript available.';
  $('audio').textContent=JSON.stringify(m.audio_signals||[],null,2);$('clip-id').textContent=c.clip_id;
  if($('audio-reason'))$('audio-reason').textContent=`Candidate reason: ${m.audio_only?'Audio only — ':''}${(m.candidate_sources||['transcript']).join(' + ')}`;
  if($('audio-events'))$('audio-events').replaceChildren(...(m.audio_events||[]).map(event=>{
    const element=text('div',`${time(event.start)}–${time(event.end)} · ${event.source_label} (${event.source})`);
    element.append(text('p',Object.entries(event.class_scores||{}).map(([label,p])=>`${label}: ${(100*p).toFixed(0)}%`).join(' · ')));
    element.append(text('p',`Relative loudness: ${event.relative_loudness_db.toFixed(1)} dB · Quiet → loud: ${event.quiet_to_loud_db.toFixed(1)} dB · Active ${event.active_seconds.toFixed(1)} s`));
    element.append(text('small',event.attribution));return element;
  }));
  document.querySelectorAll('[data-rating]').forEach(b=>{b.classList.toggle('active',b.dataset.rating===c.rating);b.setAttribute('aria-pressed',b.dataset.rating===c.rating);});
  document.querySelectorAll('.reason input').forEach(el=>el.checked=c.reasons.includes(el.value));
  $('note').value=c.note||'';$('saved').textContent=c.rating?`Saved: ${c.rating.replaceAll('_',' ').toUpperCase()}`:'Unrated';
}
function save(rating, force=false) {
  const c=clips[index];if(!c)return saving;
  if(rating!==undefined)c.rating=rating;
  c.reasons=[...document.querySelectorAll('.reason input:checked')].map(el=>el.value);c.note=$('note').value;
  const payload={rating:c.rating,reasons:[...c.reasons],note:c.note};
  const encoded=JSON.stringify(payload);if(!force&&encoded===c.savedPayload)return saving;
  $('saved').textContent='Saving…';$('error').textContent='';
  document.querySelectorAll('[data-rating]').forEach(b=>{b.classList.toggle('active',b.dataset.rating===c.rating);b.setAttribute('aria-pressed',b.dataset.rating===c.rating);});
  saving=saving.then(async()=>{await api(`/api/clips/${c.clip_id}/feedback`,payload);c.savedPayload=encoded;if(clips[index]===c){$('saved').textContent='Saved on this PC';if(c.rating)$('next').disabled=false;}try{await summary();}catch(e){error(e);}return true;}).catch(e=>{error(e);if(clips[index]===c)$('saved').textContent='Not saved — try again';return false;});
  return saving;
}
async function move(delta) {
  if(!clips[index])return;if(await save()===false)return;
  const target=delta>0?clips.slice(index+1).find(c=>!c.rating):clips.slice(0,index).reverse().find(c=>!c.rating);
  clips=clips.filter(c=>!c.rating);
  index=target?clips.indexOf(target):Math.max(0,Math.min(clips.length-1,index));render();
}
async function load() {if(await save()===false)return;const previous=clips[index]?.clip_id;clips=(await api(clipsUrl)).filter(c=>!c.rating&&(selectedSource===null||c.source_key===selectedSource));if(selectedSource!==null)scopeLabel.textContent=clips.length?`Showing clips from ${clips[0].source.filename||'the selected VOD'} only.`:'Showing clips from the selected VOD only. No qualifying unreviewed clips remain for this VOD.';index=Math.max(0,clips.findIndex(c=>c.clip_id===previous));render();await summary();}
document.querySelectorAll('[data-rating]').forEach(b=>b.onclick=()=>save(b.dataset.rating));
document.querySelectorAll('.reason input').forEach(el=>el.onchange=()=>save());
$('submit-feedback').onclick=async()=>{
  const c=clips[index];if(!c)return;
  if(!c.rating){$('submit-status').textContent='Choose a rating before submitting.';return;}
  const button=$('submit-feedback');button.disabled=true;button.textContent='Submitting…';
  $('submit-status').textContent='Saving your rating, reasons and note…';
  try{
    const success=await save(undefined,true);
    if(clips[index]===c)$('submit-status').textContent=success
      ? `✓ Feedback saved successfully at ${new Date().toLocaleTimeString()}. You can move to the next clip.`
      : 'Feedback could not be saved. Please submit again.';
  }finally{button.disabled=false;button.textContent='Submit feedback';}
};
$('previous').onclick=()=>move(-1);$('next').onclick=()=>move(1);
$('refresh').onclick=()=>load().catch(error);
$('preview').onclick=async()=>{const c=clips[index];if(!c)return;$('preview').disabled=true;$('preview-status').textContent='Preparing a local preview…';try{const result=await api(`/api/clips/${c.clip_id}/preview`,{});if(clips[index]!==c)return;$('video').src=result.url;$('video').style.display='block';$('preview-placeholder').hidden=true;}catch(e){if(clips[index]===c){$('preview-status').textContent=e.message;$('preview').disabled=false;}}};
document.addEventListener('keydown',e=>{if(['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)||e.ctrlKey||e.altKey||e.metaKey||e.repeat)return;if(ratings[Number(e.key)-1]){e.preventDefault();save(ratings[Number(e.key)-1]);}else if(e.key==='ArrowRight'){e.preventDefault();move(1);}else if(e.key==='ArrowLeft'){e.preventDefault();move(-1);}else if(e.target.tagName!=='BUTTON'&&e.code==='Space'&&$('video').getAttribute('src')){e.preventDefault();const v=$('video');v.paused?v.play().catch(error):v.pause();}});
load().catch(error);
