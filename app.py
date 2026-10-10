/* ---- Success: Thank @Name + Amount Count-up + Time ---- */
let cdTimer=null;
function onVerified(rec){
  stopPolling(); lastRecord=rec;
  el('pay-active').style.display='none';
  el('pay-success').style.display='block';

  const payer = rec.payer || 'Customer';
  const amt = Number(rec.amount);

  el('thank-name').innerText = payer;                        // 🙏 Thank @name
  countUp(el('thank-amount'), amt);                          // 💰 Amount count-up
  el('thank-time').innerText = rec.time || new Date().toLocaleTimeString('en-IN',{hour12:true}); // 🕐 Time

  const rebate=(amt*0.75/100).toFixed(2);
  el('success-details').innerHTML=`
    <div style="font-size:13px;color:var(--muted);margin-bottom:10px;">UTR: <span class="code-tag">${rec.utr}</span></div>
    <div style="font-size:13px;color:#15803d;margin-bottom:10px;font-weight:600;">🎁 কাস্টমার রিবেট: ₹${rebate}</div>`;

  playSuccessSound(); launchConfetti(); blinkTitle();
  loadStats(); loadHistory();

  // 🖥️ ১.৫ সেকেন্ড পরে অটো Fullscreen Customer Display খুলবে
  setTimeout(()=>openCustomerDisplay(payer, amt, rec.time), 1500);
}

function countUp(elm,target){
  const t0=performance.now(), dur=1000;
  function step(t){
    const p=Math.min((t-t0)/dur,1), e=1-Math.pow(1-p,3);
    elm.textContent='₹'+(target*e).toFixed(2);
    if(p<1)requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

/* ---- 🆕 Fullscreen Customer Display ---- */
function openCustomerDisplay(payer, amt, time){
  if(lastRecord){
    payer = payer || lastRecord.payer || 'Customer';
    amt = amt || Number(lastRecord.amount);
    time = time || lastRecord.time;
  }
  el('cd-merchant').innerText = el('merchant-input').value;
  el('cd-name').innerText = payer || 'Customer';
  el('cd-time').innerText = time || '';
  countUp(el('cd-amount'), amt || 0);
  el('cust-display').classList.add('open');
  playSuccessSound();
  if(cdTimer) clearTimeout(cdTimer);
  cdTimer=setTimeout(closeCustomerDisplay, 15000);   // ১৫ সেকেন্ড পরে অটো-বন্ধ
}
function closeCustomerDisplay(){
  el('cust-display').classList.remove('open');
  if(cdTimer){ clearTimeout(cdTimer); cdTimer=null; }
}
