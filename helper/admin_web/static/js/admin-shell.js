function toggleTheme(){
  var html=document.documentElement;
  var cur=html.getAttribute("data-theme")==="dark"?"dark":"light";
  var next=cur==="dark"?"light":"dark";
  html.setAttribute("data-theme",next);
  html.setAttribute("data-bs-theme",next);
  document.cookie="theme="+next+";path=/;max-age=31536000;SameSite=Lax";
  var icon=document.querySelector(".pp-theme-btn .ti");
  if(icon){icon.className="ti "+(next==="dark"?"ti-sun":"ti-moon");}
}
function showToast(message,type,duration){
  type=type||"success";
  duration=duration||4000;
  var icons={"success":"ti-circle-check","error":"ti-alert-circle","warning":"ti-alert-triangle"};
  var container=document.getElementById("pp-toast-container");
  var toast=document.createElement("div");
  toast.className="pp-toast pp-toast-"+type;
  var icon=document.createElement("i");
  icon.className="ti "+(icons[type]||icons.success);
  var msg=document.createElement("span");
  msg.className="pp-toast-msg";
  msg.textContent=message;
  var closeBtn=document.createElement("button");
  closeBtn.className="pp-toast-close";
  closeBtn.textContent="\u00d7";
  closeBtn.onclick=function(){dismissToast(toast);};
  toast.appendChild(icon);
  toast.appendChild(msg);
  toast.appendChild(closeBtn);
  container.appendChild(toast);
  requestAnimationFrame(function(){
    requestAnimationFrame(function(){toast.classList.add("show");});
  });
  setTimeout(function(){dismissToast(toast);},duration);
}
function dismissToast(el){
  if(!el||!el.parentNode)return;
  el.classList.remove("show");
  el.classList.add("hide");
  setTimeout(function(){if(el.parentNode)el.parentNode.removeChild(el);},300);
}
function setLoading(btn){
  if(!btn)return;
  btn.dataset.prevDisabled=btn.disabled?"1":"0";
  btn.classList.add("pp-loading");
  btn.disabled=true;
  var textNode=btn.querySelector(".pp-btn-text");
  if(!textNode){
    var s=document.createElement("span");
    s.className="pp-btn-text";
    while(btn.firstChild)s.appendChild(btn.firstChild);
    btn.appendChild(s);
  }
}
function clearLoading(btn){
  if(!btn)return;
  btn.classList.remove("pp-loading");
  btn.disabled=btn.dataset.prevDisabled==="1";
  delete btn.dataset.prevDisabled;
}
async function readAdminPayload(response,fallbackMessage){
  var text=await response.text();
  if(!text)return {};
  try{
    return JSON.parse(text);
  }catch(_err){
    throw new Error(fallbackMessage||"Request failed");
  }
}
function adminPayloadError(payload,fallbackMessage){
  if(payload&&typeof payload.error==="string"&&payload.error)return payload.error;
  if(payload&&payload.error&&typeof payload.error.message==="string"&&payload.error.message)return payload.error.message;
  if(payload&&typeof payload.message==="string"&&payload.message)return payload.message;
  return fallbackMessage||"Request failed";
}
async function adminFetch(url,options,fallbackMessage){
  options=options||{};
  var headers=new Headers(options.headers||{});
  if(!headers.has("X-Requested-With"))headers.set("X-Requested-With","XMLHttpRequest");
  var fetchOptions={method:options.method||"GET",headers:headers};
  if(Object.prototype.hasOwnProperty.call(options,"body"))fetchOptions.body=options.body;
  if(Object.prototype.hasOwnProperty.call(options,"credentials"))fetchOptions.credentials=options.credentials;
  var response=await fetch(url,fetchOptions);
  var payload=await readAdminPayload(response,fallbackMessage);
  if(!response.ok||payload.ok===false){
    var error=new Error(adminPayloadError(payload,fallbackMessage));
    error.payload=payload;
    throw error;
  }
  return payload;
}
function adminPostForm(form,fallbackMessage){
  return adminFetch(form.action,{
    method:"POST",
    body:new FormData(form)
  },fallbackMessage);
}
(function(){
  var params=new URLSearchParams(window.location.search);
  var msg=params.get("msg");
  var error=params.get("error");
  var flashType=params.get("type");
  if(msg){showToast(decodeURIComponent(msg.replace(/\+/g," ")),flashType||"success");}
  if(error){showToast(decodeURIComponent(error.replace(/\+/g," ")),"error",6000);}
  if(msg||error){
    params.delete("msg");params.delete("error");params.delete("type");
    var clean=window.location.pathname;
    var qs=params.toString();
    if(qs)clean+="?"+qs;
    window.history.replaceState(null,"",clean);
  }
})();
