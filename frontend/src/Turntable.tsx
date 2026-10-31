import {useEffect,useRef,useState} from 'react';
import * as T from 'three';
import {RoundedBoxGeometry} from 'three/addons/geometries/RoundedBoxGeometry.js';
import type {Song} from './types';

type Quality='full'|'light'|'static';
type Props={song?:Song|null;playing:boolean;verified:boolean;reduced?:boolean};
/** The scene follows confirmed state. Pointer movement changes only the camera. */
export default function Turntable({song,playing,verified,reduced=false}:Props){
 const host=useRef<HTMLDivElement>(null),motion=useRef({playing,verified,reduced}),record=useRef<T.Group|null>(null);
 const [quality,setQuality]=useState<Quality>(()=>{try{return (localStorage.getItem('cove.render') as Quality)||'full'}catch{return 'full'}});
 const [fallback,setFallback]=useState(false),[reduction,setReduction]=useState(''),[systemReduced,setSystemReduced]=useState(false);
 const cover=song?.cover_url||song?.coverUrl||'';
 motion.current={playing,verified,reduced:reduced||systemReduced};
 useEffect(()=>{if(!window.matchMedia)return;const m=matchMedia('(prefers-reduced-motion: reduce)'),change=()=>setSystemReduced(m.matches);change();m.addEventListener('change',change);return()=>m.removeEventListener('change',change)},[]);
 useEffect(()=>{
  const el=host.current;if(!el||quality==='static')return;
  let renderer:T.WebGLRenderer;
  try{renderer=new T.WebGLRenderer({antialias:quality==='full',alpha:true,powerPreference:'high-performance',precision:quality==='full'?'highp':'mediump'});}catch{setFallback(true);return}
  setFallback(false);
  // The album label is the focal point. Keep enough physical pixels for cover art
  // on HiDPI displays while retaining the light mode fallback on weaker devices.
  renderer.setPixelRatio(Math.min(devicePixelRatio||1,quality==='full'?2.25:1.35));
  renderer.shadowMap.enabled=quality==='full';renderer.shadowMap.type=T.PCFSoftShadowMap;renderer.outputColorSpace=T.SRGBColorSpace;
  renderer.toneMapping=T.ACESFilmicToneMapping;renderer.toneMappingExposure=1.4;el.appendChild(renderer.domElement);
  renderer.domElement.setAttribute('aria-label','可拖动观察角度的三维黑胶唱机');renderer.domElement.setAttribute('role','img');
  const scene=new T.Scene(),camera=new T.PerspectiveCamera(28,1,.1,80);
  camera.up.set(0,0,-1);camera.position.set(0,10.4,1.2);camera.lookAt(0,0,0);
  const root=new T.Group();scene.add(root);
  const metal=new T.MeshStandardMaterial({color:0xbcc1c5,metalness:.85,roughness:.25});
  const dark=new T.MeshStandardMaterial({color:0x181b1d,metalness:.4,roughness:.38});
  function mesh(g:T.BufferGeometry,m:T.Material,x=0,y=0,z=0,parent:T.Object3D=root){const v=new T.Mesh(g,m);v.position.set(x,y,z);v.castShadow=true;v.receiveShadow=true;parent.add(v);return v}
  // The listening view is intentionally only the record and tonearm. The
  // surrounding workbench already supplies the stage; removing the wooden
  // deck gives album art more pixels and keeps the object visually quiet.
  const discCenter=0;
  mesh(new T.CylinderGeometry(2.62,2.62,.17,144),metal,discCenter,.08,0);
  mesh(new T.CylinderGeometry(2.55,2.55,.10,144),dark,discCenter,.20,0);
  const disc=new T.Group();disc.position.set(discCenter,.28,0);root.add(disc);record.current=disc;
  const canvas=document.createElement('canvas');canvas.width=canvas.height=1024;const ctx=canvas.getContext('2d')!;
  ctx.fillStyle='#181a1b';ctx.fillRect(0,0,1024,1024);
  for(let i=180;i<509;i+=2.4){ctx.beginPath();ctx.arc(512,512,i,0,Math.PI*2);ctx.strokeStyle=i%7<3?'#555a5b':'#222627';ctx.lineWidth=.75;ctx.stroke()}
  const groove=new T.CanvasTexture(canvas);groove.colorSpace=T.SRGBColorSpace;
  const face=new T.MeshStandardMaterial({map:groove,bumpMap:groove,bumpScale:.016,roughness:.28,metalness:.55});
  const top=mesh(new T.CircleGeometry(2.485,144),face,0,.024,0,disc);top.rotation.x=-Math.PI/2;
  // A larger, self-lit label keeps the actual album art legible instead of
  // letting the deck lighting turn it into a small muddy spot.
  const labelMaterial=new T.MeshBasicMaterial({color:0xffffff});
  const label=mesh(new T.CircleGeometry(1.60,128),labelMaterial,0,.058,0,disc);label.rotation.x=-Math.PI/2;
  label.name='album-label';
  const labelRing=new T.Mesh(new T.RingGeometry(1.60,1.69,128),new T.MeshStandardMaterial({color:0x151718,metalness:.6,roughness:.25}));labelRing.rotation.x=-Math.PI/2;labelRing.position.y=.052;labelRing.castShadow=true;disc.add(labelRing);
  mesh(new T.CylinderGeometry(.055,.068,.21,24),metal,discCenter,.39,0);
  // S-shaped metal tonearm, counterweight and a separate cartridge.
  const arm=new T.Group();arm.position.set(2.55,.22,-1.35);root.add(arm);
  mesh(new T.CylinderGeometry(.28,.33,.25,32),dark,0,0,0,arm);
  mesh(new T.CylinderGeometry(.18,.18,.32,32),metal,0,.16,0,arm);
  const curve=new T.CatmullRomCurve3([new T.Vector3(0,.36,-.42),new T.Vector3(0,.36,.15),new T.Vector3(-.12,.34,.84),new T.Vector3(-.65,.27,1.68),new T.Vector3(-.89,.25,1.86)]);
  mesh(new T.TubeGeometry(curve,32,.047,12,false),metal,0,0,0,arm);
  const counter=mesh(new T.CylinderGeometry(.17,.17,.32,24),dark,0,.36,-.44,arm);counter.rotation.x=Math.PI/2;
  const head=mesh(new RoundedBoxGeometry(.19,.1,.38,2,.025),dark,-.94,.24,1.95,arm);head.rotation.y=-.45;
  mesh(new T.ConeGeometry(.015,.12,8),metal,-.94,.12,1.95,arm).rotation.z=Math.PI;
  scene.add(new T.HemisphereLight(0xe6e9ee,0x31261f,2.7));
  const key=new T.DirectionalLight(0xffe2b8,5.8);key.position.set(-4,8,3);key.castShadow=false;scene.add(key);
  const rim=new T.DirectionalLight(0xa7bcd0,3.8);rim.position.set(4,3,-4);scene.add(rim);
  let disposed=false,frame=0,last=0,speed=0,total=0,slow=0,frames=0,visible=!document.hidden,drag=false,px=0,py=0,yaw=0,pitch=0;
  const ro=new ResizeObserver(()=>{const w=el.clientWidth,h=el.clientHeight;if(!w||!h)return;renderer.setSize(w,h,false);camera.aspect=w/h;camera.position.set(0,w<500?10.8:10.4,w<500?1.5:1.2);camera.updateProjectionMatrix();renderStill()});ro.observe(el);
  function renderStill(){if(!disposed)renderer.render(scene,camera)}
  const loss=(e:Event)=>{e.preventDefault();setFallback(true);cancelAnimationFrame(frame)};
  renderer.domElement.addEventListener('webglcontextlost',loss);
  function loop(at:number){if(disposed||!visible)return;const dt=last?Math.min((at-last)/1000,.08):0;last=at;const m=motion.current;
   const target=m.playing&&m.verified&&!m.reduced?3.49:0;speed+=(target-speed)*Math.min(1,dt*2.8);disc.rotation.y-=speed*dt;
   root.rotation.y+=(yaw-root.rotation.y)*.08;root.rotation.x+=(pitch-root.rotation.x)*.08;
   arm.rotation.y+=(m.playing&&m.verified?-.12-arm.rotation.y:.23-arm.rotation.y)*.025;
   renderer.render(scene,camera);total+=dt;frames++;if(dt>.045)slow++;
   if(total>6&&frames>60){if(slow/frames>.6&&quality==='full'){setReduction('已按设备性能切换轻量立体');setQuality('light');return}total=slow=frames=0}
   frame=requestAnimationFrame(loop);
  }
  const visibility=()=>{visible=!document.hidden;last=0;cancelAnimationFrame(frame);if(visible)frame=requestAnimationFrame(loop)};
  const down=(e:PointerEvent)=>{drag=true;px=e.clientX;py=e.clientY;renderer.domElement.setPointerCapture(e.pointerId)};
  const move=(e:PointerEvent)=>{if(motion.current.reduced)return;if(drag){yaw=Math.max(-.5,Math.min(.5,yaw+(e.clientX-px)*.004));pitch=Math.max(-.12,Math.min(.22,pitch+(e.clientY-py)*.003));px=e.clientX;py=e.clientY}else{const r=el.getBoundingClientRect();yaw=((e.clientX-r.left)/r.width-.5)*.08}};
  const up=()=>{drag=false};renderer.domElement.addEventListener('pointerdown',down);renderer.domElement.addEventListener('pointermove',move);renderer.domElement.addEventListener('pointerup',up);document.addEventListener('visibilitychange',visibility);
  frame=requestAnimationFrame(loop);
  return()=>{disposed=true;record.current=null;cancelAnimationFrame(frame);ro.disconnect();document.removeEventListener('visibilitychange',visibility);renderer.domElement.removeEventListener('webglcontextlost',loss);scene.traverse(o=>{if(o instanceof T.Mesh){o.geometry.dispose();const mats=Array.isArray(o.material)?o.material:[o.material];mats.forEach(m=>{for(const value of Object.values(m))if(value instanceof T.Texture)value.dispose();m.dispose()})}});renderer.dispose();renderer.domElement.remove()};
 },[quality]);
 useEffect(()=>{let disposed=false;const disc=record.current;if(!disc)return;const material=(disc.getObjectByName('album-label') as T.Mesh).material as T.MeshStandardMaterial;
  material.map?.dispose();material.map=null;material.color.set(0xb8a48b);material.needsUpdate=true;
  if(cover){
   const hiRes=/(music\.126\.net|music\.netease\.com)/i.test(cover)&&!/[?&]param=/.test(cover)?`${cover}${cover.includes('?')?'&':'?'}param=1024y1024`:cover;
   new T.TextureLoader().setCrossOrigin('anonymous').load(hiRes,texture=>{if(disposed){texture.dispose();return}texture.colorSpace=T.SRGBColorSpace;texture.anisotropy=8;texture.minFilter=T.LinearMipmapLinearFilter;texture.magFilter=T.LinearFilter;material.map=texture;material.color.set(0xffffff);material.needsUpdate=true},undefined,()=>{});
  }
  return()=>{disposed=true};
 },[cover,quality,fallback]);
 const staticView=quality==='static'||fallback;
 return <div className="turntable-room" data-playing={playing&&verified}><div className="table-light" aria-hidden="true"/><div ref={host} className={'turntable-canvas '+(staticView?'canvas-hidden':'')} />{staticView&&<div className="css-turntable"><div className={'css-disc '+(quality!=='static'&&playing&&verified&&!reduced&&!systemReduced?'rotating':'')}><div className="css-label">{cover&&<img src={cover} alt="唱片中心封面"/>}<i/></div></div><div className="css-arm"/></div>}<div className="deck-caption"><span>{!verified?'等待核对':playing?'唱片转动中':'唱针暂歇'}</span><label>画面<select aria-label="唱机画质" value={quality} onChange={e=>{const q=e.target.value as Quality;setQuality(q);try{localStorage.setItem('cove.render',q)}catch{/* preference optional */}}}><option value="full">完整立体</option><option value="light">轻量立体</option><option value="static">静态唱机</option></select></label></div>{(fallback||reduction)&&<small className="render-note">{fallback?'此设备暂不可用 WebGL，已显示 CSS 唱机。':reduction}</small>}</div>
}
