import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { TrackballControls } from "three/addons/controls/TrackballControls.js";
import { colorForClass, hexToCss } from "./classColors";

const SPHERE_RADIUS = 5;
const REFERENCE_N = 2000;
const REFERENCE_DOT_RADIUS = 0.09;
const SPIN_SPEED = 0.05;
const IDLE_RESUME_MS = 3000;
const DRAG_CLICK_THRESHOLD_PX = 6;

function fibonacciSphere(n, radius = 1) {
  const pts = [];
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = n > 1 ? 1 - (i / (n - 1)) * 2 : 0;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = goldenAngle * i;
    pts.push(new THREE.Vector3(Math.cos(theta) * r * radius, y * radius, Math.sin(theta) * r * radius));
  }
  return pts;
}

// Builds everything the scene needs from real episode data.
//
// The core idea, and the one that finally works: ONE uniform golden-angle
// Fibonacci lattice of EXACTLY the real episode count -- every lattice
// point becomes exactly one real episode's dot, none generated per-class,
// none discarded. Uniform density across the whole sphere is therefore
// structural, not something a placement heuristic has to achieve.
//
// The only real problem left is making each class's REGION capture
// exactly its own real episode count. Two steps do that:
//
//   1. Converge per-class weights. Region i is "angle to seed i divided
//      by weight i" (so a bigger weight = a bigger region). Weights start
//      from the area formula and are nudged multiplicatively until each
//      region's captured point count matches that class's real count
//      (lands within +/-2 in ~20ms, measured on the real 12-class data).
//   2. Capacity-constrained greedy assignment closes the last +/-2
//      EXACTLY: score every (point, class) pair, sort ascending, and walk
//      the list assigning each point to its best-scoring class that still
//      has room. Exact per-class counts by construction, every point
//      placed, and it only moves ~5 of 2144 points (0.23%, measured) off
//      the plain argmin the tint uses -- an invisible boundary
//      difference, unlike a whole missing band.
//
// An earlier version oversampled the lattice ~1.5x and trimmed each
// bucket back down to size by dropping the "most crowded" points. That
// was the real cause of the large empty swaths visible in every single
// region: in a uniform lattice nearly every point has an identical
// nearest-neighbor distance, so that sort was almost entirely ties, and
// JS resolves ties by original array order -- which is Fibonacci spiral
// order, i.e. pole-to-pole by latitude. Slicing the first n therefore cut
// a CONTIGUOUS LATITUDE BAND out of every region instead of thinning it
// evenly. No trimming happens anywhere now.
function buildLayout(episodes) {
  const classes = [...new Set(episodes.map((e) => e.fault_class))].sort();
  const classIndex = Object.fromEntries(classes.map((c, i) => [c, i]));
  const total = episodes.length;
  const C = classes.length;

  const seeds = fibonacciSphere(C, 1);
  const indicesByClass = classes.map(() => []);
  episodes.forEach((e, i) => indicesByClass[classIndex[e.fault_class]].push(i));
  const targets = indicesByClass.map((idxs) => idxs.length);

  const dotPositions = new Array(total);

  if (C < 3) {
    fibonacciSphere(total, SPHERE_RADIUS).forEach((p, i) => { dotPositions[i] = p; });
    const capAngles = targets.map(() => Math.PI);
    return { classes, seeds, indicesByClass, dotPositions, classify: () => 0, capAngles };
  }

  const lattice = fibonacciSphere(total, 1);
  // angle from every lattice point to every seed, computed once
  const angles = lattice.map((p) => seeds.map((s) => Math.acos(Math.max(-1, Math.min(1, p.dot(s))))));

  // area-based starting weights: area_i = 4*pi * n_i/N
  let weights = targets.map((n) => Math.acos(Math.max(-1, Math.min(1, 1 - 2 * (n / total)))));

  function countsFor(w) {
    const bc = new Array(C).fill(0);
    for (let k = 0; k < total; k++) {
      let best = 0, bestScore = Infinity;
      const ak = angles[k];
      for (let i = 0; i < C; i++) { const s = ak[i] / w[i]; if (s < bestScore) { bestScore = s; best = i; } }
      bc[best]++;
    }
    return bc;
  }

  let bc = countsFor(weights);
  for (let iter = 0; iter < 120; iter++) {
    if (targets.every((t, i) => bc[i] === t)) break;
    weights = weights.map((w, i) => w * Math.pow(targets[i] / Math.max(bc[i], 1), 0.15));
    bc = countsFor(weights);
  }

  // capacity-constrained greedy -> exact per-class counts
  //
  // Candidates are limited to each point's few NEAREST classes (not all C
  // of them) -- with every class as a candidate, a point deep inside one
  // region could get bumped to a totally unrelated, distant class just
  // because that class still had room when this point's turn came up in the
  // global sort, producing a visibly stray, wrong-territory dot. Capping the
  // candidate list to genuine geometric neighbors means a reassignment can
  // only ever move a point to an ADJACENT region, never across the sphere.
  const K_CANDIDATES = Math.min(C, 4);
  const triples = [];
  for (let k = 0; k < total; k++) {
    const ak = angles[k];
    const nearest = ak
      .map((a, i) => [a / weights[i], i])
      .sort((a, b) => a[0] - b[0])
      .slice(0, K_CANDIDATES);
    nearest.forEach(([s, i]) => triples.push([s, k, i]));
  }
  triples.sort((a, b) => a[0] - b[0]);

  const pointsByClass = classes.map(() => []);
  const room = targets.slice();
  const taken = new Uint8Array(total);
  let placed = 0;
  for (let t = 0; t < triples.length && placed < total; t++) {
    const [, k, i] = triples[t];
    if (taken[k] || room[i] === 0) continue;
    taken[k] = 1; room[i]--; placed++;
    pointsByClass[i].push(lattice[k]);
  }

  indicesByClass.forEach((idxs, ci) => {
    idxs.forEach((epIdx, k) => {
      const p = pointsByClass[ci][k] ?? seeds[ci];
      dotPositions[epIdx] = p.clone().multiplyScalar(SPHERE_RADIUS);
    });
  });

  // Region tint uses the SAME converged weights, so tinted area and dot
  // territory are the same partition (up to the ~0.23% of points the
  // exact-count greedy step moves off the raw argmin).
  const capAngles = weights;
  function classify(unitPoint) {
    let best = 0, bestScore = Infinity;
    for (let i = 0; i < C; i++) {
      const angle = Math.acos(Math.max(-1, Math.min(1, unitPoint.dot(seeds[i]))));
      const score = angle / weights[i];
      if (score < bestScore) { bestScore = score; best = i; }
    }
    return best;
  }

  return { classes, seeds, indicesByClass, dotPositions, classify, capAngles };
}

export default function DotSphere({ episodes, selectedId, onSelect, filterClass, onFilterClass, height }) {
  const containerRef = useRef(null);
  const stateRef = useRef({});
  const [hudVisible, setHudVisible] = useState(true);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || episodes.length === 0) return undefined;

    const { classes, seeds, indicesByClass, classify, dotPositions } = buildLayout(episodes);

    const width = container.clientWidth, initialHeight = container.clientHeight;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0a0e14);
    const camera = new THREE.PerspectiveCamera(50, width / initialHeight, 0.1, 100);
    // z=9 (old) was mathematically too close to fit the whole radius-5
    // sphere at this 50deg FOV -- vertical half-extent visible at z=9 is
    // only ~4.2 units (tan(25deg)*9), less than the sphere's own radius, so
    // every fresh load started cropped/zoomed-in. z=14 gives ~30% margin
    // around the full sphere (tan(25deg)*14 ~= 6.5, comfortably > radius 5).
    camera.position.set(0, 0, 14);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(width, initialHeight);
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    container.appendChild(renderer.domElement);

    // Cached, not read live on every pointer event -- getBoundingClientRect()
    // forces a synchronous layout, and onCanvasPointerMove fires on every
    // mouse movement over the canvas (not just clicks/drags), so calling it
    // there meant just hovering the sphere forced a layout recalc on top of
    // updateLabels()'s per-frame style writes. Only real resize/scroll can
    // actually move the canvas, so those are the only things that need to
    // recompute it.
    let canvasRect = renderer.domElement.getBoundingClientRect();
    function updateCanvasRect() { canvasRect = renderer.domElement.getBoundingClientRect(); }
    window.addEventListener("scroll", updateCanvasRect, { passive: true, capture: true });

    (function drawStarfield() {
      const STAR_COUNT = 800;
      const positions = new Float32Array(STAR_COUNT * 3);
      for (let i = 0; i < STAR_COUNT; i++) {
        const theta = Math.random() * Math.PI * 2, phi = Math.acos(2 * Math.random() - 1);
        const r = 45 + Math.random() * 15;
        positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
        positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
        positions[i * 3 + 2] = r * Math.cos(phi);
      }
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
      // sizeAttenuation:false (default is true) -- stars sit 45-60 units out
      // while the camera only ever gets to 6-30 units away, so with
      // attenuation on, `size` (world units) shrunk to sub-pixel at that
      // distance and the starfield was effectively invisible even though it
      // was really being rendered. With attenuation off, `size` is a fixed
      // screen-space pixel size regardless of distance -- the standard fix
      // for a background starfield.
      const mat = new THREE.PointsMaterial({
        color: 0xc8d2e0, size: 3, sizeAttenuation: false,
        transparent: true, opacity: 0.8, depthWrite: false,
      });
      scene.add(new THREE.Points(geo, mat));
    })();

    const labelContainer = document.createElement("div");
    labelContainer.style.position = "absolute";
    labelContainer.style.inset = "0";
    labelContainer.style.pointerEvents = "none";
    container.appendChild(labelContainer);
    const labelEls = classes.map((c) => {
      const el = document.createElement("div");
      el.textContent = c;
      Object.assign(el.style, {
        position: "absolute",
        color: hexToCss(colorForClass(c)),
        fontSize: "11px",
        fontWeight: "bold",
        fontFamily: "monospace",
        textShadow: "0 0 4px #000, 0 0 8px #000",
        transform: "translate(-50%, -50%)",
        whiteSpace: "nowrap",
        pointerEvents: "none",
      });
      labelContainer.appendChild(el);
      return el;
    });
    // Measured ONCE here, not per-frame -- offsetWidth/offsetHeight force a
    // synchronous layout reflow, and reading them from every label on every
    // animation frame (60x/sec, on top of an already-heavy WebGL scene) is
    // what caused the tab to hang on zoom/select/rotate. Text never changes
    // after mount, so the size never needs re-measuring.
    const labelDims = labelEls.map((el) => ({ w: el.offsetWidth, h: el.offsetHeight }));
    const _camDirTmp = new THREE.Vector3();
    // Cached, not read live from the DOM every frame -- this function also
    // WRITES labelEls[i].style.left/top every frame, and reading
    // clientWidth/clientHeight right after a style write forces a
    // synchronous layout recalculation (classic layout-thrashing pattern),
    // 60x/sec, forever. Confirmed via Chrome DevTools profiling: this single
    // line alone accounted for ~1.1s of a 16.5s recording (get clientWidth +
    // Layout + Recalculate style, all attributed to this call site) -- the
    // real dominant cause of the hang. Kept in sync via the existing
    // ResizeObserver below instead.
    let containerW = width, containerH = initialHeight;
    function updateLabels() {
      const w = containerW, h = containerH;
      const camDir = _camDirTmp.copy(camera.position).normalize();
      seeds.forEach((p, i) => {
        const worldPos = p.clone().multiplyScalar(SPHERE_RADIUS * 1.05).applyQuaternion(worldGroup.quaternion);
        const visible = worldPos.clone().normalize().dot(camDir) > 0.15;
        if (!visible) { labelEls[i].style.display = "none"; return; }
        const projected = worldPos.clone().project(camera);
        if (projected.z > 1) { labelEls[i].style.display = "none"; return; }
        labelEls[i].style.display = "block";
        const halfW = labelDims[i].w / 2 + 2;
        const halfH = labelDims[i].h / 2 + 2;
        const x = Math.min(w - halfW, Math.max(halfW, (projected.x * 0.5 + 0.5) * w));
        const y = Math.min(h - halfH, Math.max(halfH, (-projected.y * 0.5 + 0.5) * h));
        labelEls[i].style.left = `${x}px`;
        labelEls[i].style.top = `${y}px`;
      });
    }

    const controls = new TrackballControls(camera, renderer.domElement);
    controls.rotateSpeed = 3.0;
    controls.noZoom = false;
    controls.noPan = true;
    controls.minDistance = 6;
    controls.maxDistance = 30;
    controls.dynamicDampingFactor = 0.15;

    let isInteracting = false;
    let hasSelection = false;
    let lastInteractionTime = -Infinity;
    let pointerDownPos = null;
    let pinnedLegendClass = null;
    let selectedGlobalIdx = null;

    const worldGroup = new THREE.Group();
    scene.add(worldGroup);

    const core = new THREE.Mesh(
      new THREE.SphereGeometry(SPHERE_RADIUS * 0.96, 48, 48),
      new THREE.MeshBasicMaterial({ color: 0x11161f })
    );
    worldGroup.add(core);

    // Region tint: every face gets exactly one class via the same
    // classify() function dot placement used above, so tint and dots can
    // never disagree. Always defined for every face (plain argmin over a
    // fixed seed set) -- whole-sphere coverage, no gaps, no overlaps.
    if (classes.length >= 3) {
      const FILL_RADIUS = SPHERE_RADIUS * 0.985;
      // Higher tessellation than before (was 160x90) -- each face is colored
      // as ONE flat color from its centroid's nearest region, so a coarser
      // grid means a single larger triangle can straddle a boundary and
      // render as a visibly blocky notch rather than a clean edge. This is a
      // one-time build cost (not per-frame), already confirmed not to be the
      // scene's performance bottleneck, so raising it is safe.
      const geo = new THREE.SphereGeometry(FILL_RADIUS, 260, 150).toNonIndexed();
      const pos = geo.attributes.position;
      const colors = new Float32Array(pos.count * 3);
      const v = new THREE.Vector3();
      const faceColor = new THREE.Color();
      for (let f = 0; f < pos.count; f += 3) {
        v.set(
          (pos.getX(f) + pos.getX(f + 1) + pos.getX(f + 2)) / 3,
          (pos.getY(f) + pos.getY(f + 1) + pos.getY(f + 2)) / 3,
          (pos.getZ(f) + pos.getZ(f + 1) + pos.getZ(f + 2)) / 3
        ).normalize();
        faceColor.set(colorForClass(classes[classify(v)]));
        for (let k = 0; k < 3; k++) {
          colors[(f + k) * 3] = faceColor.r;
          colors[(f + k) * 3 + 1] = faceColor.g;
          colors[(f + k) * 3 + 2] = faceColor.b;
        }
      }
      geo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
      // depthWrite:true (was false) -- this mesh is functionally opaque
      // coverage (every visible-hemisphere pixel gets exactly one face, no
      // other transparent object needs to blend "through" it), so it should
      // resolve occlusion via the real depth buffer per-pixel rather than
      // relying on Three's coarse whole-object transparency sort. With
      // depthWrite off, at a dense multi-region boundary junction the sort
      // could pick the wrong triangle order for a small patch and let the
      // dark core underneath show through -- the persistent notch.
      worldGroup.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.4, depthWrite: true })));
    }

    const TOTAL_DOTS = episodes.length;
    const DOT_RADIUS = Math.min(0.14, Math.max(0.025, REFERENCE_DOT_RADIUS * Math.sqrt(REFERENCE_N / TOTAL_DOTS)));
    const HIT_RADIUS = Math.max(DOT_RADIUS, 0.05);

    const dotGeo = new THREE.SphereGeometry(DOT_RADIUS, 8, 8);
    const hitGeo = new THREE.SphereGeometry(HIT_RADIUS, 6, 6);
    const dummy = new THREE.Object3D();
    const instMeshesByClass = [];
    const hitMeshesByClass = [];
    const nnLineMatsByClass = [];

    classes.forEach((className, ci) => {
      const idxs = indicesByClass[ci];
      const visMat = new THREE.MeshBasicMaterial({ color: colorForClass(className), transparent: true, opacity: 1 });
      const mesh = new THREE.InstancedMesh(dotGeo, visMat, Math.max(idxs.length, 1));
      const hitMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false });
      const hitMesh = new THREE.InstancedMesh(hitGeo, hitMat, Math.max(idxs.length, 1));

      idxs.forEach((dotIdx, localIdx) => {
        dummy.position.copy(dotPositions[dotIdx]);
        dummy.updateMatrix();
        mesh.setMatrixAt(localIdx, dummy.matrix);
        hitMesh.setMatrixAt(localIdx, dummy.matrix);
      });
      mesh.instanceMatrix.needsUpdate = true;
      hitMesh.instanceMatrix.needsUpdate = true;
      mesh.userData.localToGlobal = idxs;
      hitMesh.userData.localToGlobal = idxs;
      worldGroup.add(mesh);
      worldGroup.add(hitMesh);
      instMeshesByClass.push(mesh);
      hitMeshesByClass.push(hitMesh);

      if (idxs.length >= 2) {
        const pts = idxs.map((gi) => dotPositions[gi]);
        const edgeKeys = new Set();
        const positions = [];
        pts.forEach((p, i) => {
          const nearest = pts
            .map((q, j) => [j === i ? Infinity : p.distanceToSquared(q), j])
            .sort((a, b) => a[0] - b[0])
            .slice(0, 3);
          nearest.forEach(([, j]) => {
            const key = i < j ? `${i}_${j}` : `${j}_${i}`;
            if (edgeKeys.has(key)) return;
            edgeKeys.add(key);
            positions.push(p.x, p.y, p.z, pts[j].x, pts[j].y, pts[j].z);
          });
        });
        const geo = new THREE.BufferGeometry();
        geo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
        const mat = new THREE.LineBasicMaterial({ color: colorForClass(className), transparent: true, opacity: 0.3, depthWrite: false });
        worldGroup.add(new THREE.LineSegments(geo, mat));
        nnLineMatsByClass.push(mat);
      } else {
        nnLineMatsByClass.push(null);
      }

    });

    const haloGeo = new THREE.RingGeometry(DOT_RADIUS * 1.75, DOT_RADIUS * 1.95, 32);
    const haloMat = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 1, side: THREE.DoubleSide, depthWrite: false, blending: THREE.AdditiveBlending });
    const selectionHalo = new THREE.Mesh(haloGeo, haloMat);
    selectionHalo.visible = false;
    worldGroup.add(selectionHalo);

    const hoverGeo = new THREE.RingGeometry(DOT_RADIUS * 1.4, DOT_RADIUS * 1.55, 24);
    const hoverMat = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.45, side: THREE.DoubleSide, depthWrite: false });
    const hoverRing = new THREE.Mesh(hoverGeo, hoverMat);
    hoverRing.visible = false;
    worldGroup.add(hoverRing);

    function orientRingAt(ring, localPos, radialOffset) {
      const outward = localPos.clone().normalize();
      ring.position.copy(outward.clone().multiplyScalar(SPHERE_RADIUS + radialOffset));
      ring.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), outward);
    }

    let focusAnim = null;
    const clock = new THREE.Clock();
    function focusToward(localDirUnit) {
      const camDir = camera.position.clone().normalize();
      const targetQuat = new THREE.Quaternion().setFromUnitVectors(localDirUnit, camDir);
      focusAnim = { from: worldGroup.quaternion.clone(), to: targetQuat, start: clock.getElapsedTime(), duration: 0.7 };
    }

    function setLegendHighlight(ci) {
      instMeshesByClass.forEach((mesh, i) => {
        const dim = ci !== null && i !== ci;
        mesh.material.opacity = dim ? 0.15 : 1;
        if (nnLineMatsByClass[i]) nnLineMatsByClass[i].opacity = dim ? 0.05 : 0.3;
      });
    }

    function selectByGlobalIdx(globalIdx) {
      hasSelection = true;
      selectedGlobalIdx = globalIdx;
      orientRingAt(selectionHalo, dotPositions[globalIdx], 0.015);
      selectionHalo.visible = true;
      focusToward(dotPositions[globalIdx].clone().normalize());
      onSelect(episodes[globalIdx].episode_id);
    }

    function deselect() {
      selectionHalo.visible = false;
      hasSelection = false;
      selectedGlobalIdx = null;
      lastInteractionTime = performance.now();
    }

    const raycaster = new THREE.Raycaster();
    const mouse = new THREE.Vector2();

    function onPointerDown(e) {
      isInteracting = true;
      lastInteractionTime = performance.now();
      pointerDownPos = { x: e.clientX, y: e.clientY };
    }
    function onPointerUpGlobal() {
      // Only reset the idle timer if a canvas drag was actually in progress --
      // this listener is global (window-level, to catch a drag release outside
      // the canvas), so it also fires for clicks on unrelated page UI like the
      // HUD toggle button, which was wrongly pausing the auto-spin every time.
      if (isInteracting) lastInteractionTime = performance.now();
      isInteracting = false;
    }
    function onCanvasPointerUp(e) {
      if (!pointerDownPos) return;
      const moved = Math.hypot(e.clientX - pointerDownPos.x, e.clientY - pointerDownPos.y);
      pointerDownPos = null;
      if (moved > DRAG_CLICK_THRESHOLD_PX) return;
      mouse.x = ((e.clientX - canvasRect.left) / canvasRect.width) * 2 - 1;
      mouse.y = -((e.clientY - canvasRect.top) / canvasRect.height) * 2 + 1;
      raycaster.setFromCamera(mouse, camera);
      const hit = raycaster.intersectObjects([core, ...hitMeshesByClass])[0];
      if (!hit) { deselect(); onSelect(null); return; }
      if (hit.object === core || hit.instanceId === undefined) return;
      const globalIdx = hit.object.userData.localToGlobal[hit.instanceId];
      if (globalIdx === selectedGlobalIdx) { deselect(); onSelect(null); return; } // reclicking the selected dot toggles it off
      selectByGlobalIdx(globalIdx);
    }
    function onCanvasPointerMove(e) {
      if (isInteracting) { hoverRing.visible = false; return; }
      mouse.x = ((e.clientX - canvasRect.left) / canvasRect.width) * 2 - 1;
      mouse.y = -((e.clientY - canvasRect.top) / canvasRect.height) * 2 + 1;
      raycaster.setFromCamera(mouse, camera);
      const hit = raycaster.intersectObjects([core, ...hitMeshesByClass])[0];
      if (!hit || hit.object === core || hit.instanceId === undefined) { hoverRing.visible = false; return; }
      const globalIdx = hit.object.userData.localToGlobal[hit.instanceId];
      if (globalIdx === selectedGlobalIdx) { hoverRing.visible = false; return; }
      orientRingAt(hoverRing, dotPositions[globalIdx], 0.01);
      hoverRing.visible = true;
    }

    renderer.domElement.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointerup", onPointerUpGlobal);
    window.addEventListener("pointercancel", onPointerUpGlobal);
    renderer.domElement.addEventListener("pointerup", onCanvasPointerUp);
    renderer.domElement.addEventListener("pointermove", onCanvasPointerMove);

    function handleResize() {
      const w = container.clientWidth, h = container.clientHeight;
      containerW = w; containerH = h;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
      controls.handleResize();
      updateCanvasRect();
    }
    const resizeObserver = new ResizeObserver(handleResize);
    resizeObserver.observe(container);

    let raf;
    function animate() {
      raf = requestAnimationFrame(animate);
      const delta = clock.getDelta();
      if (!isInteracting && !hasSelection && pinnedLegendClass === null && performance.now() - lastInteractionTime > IDLE_RESUME_MS) {
        const axis = camera.up.clone().normalize();
        worldGroup.rotateOnWorldAxis(axis, SPIN_SPEED * delta);
      }
      if (focusAnim) {
        const t = Math.min(1, (clock.getElapsedTime() - focusAnim.start) / focusAnim.duration);
        const eased = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
        worldGroup.quaternion.slerpQuaternions(focusAnim.from, focusAnim.to, eased);
        if (t >= 1) focusAnim = null;
      }
      if (selectionHalo.visible) {
        const t = clock.getElapsedTime();
        const pulse = 1 + 0.4 * (0.5 + 0.5 * Math.sin(t * 4));
        selectionHalo.scale.set(pulse, pulse, pulse);
        haloMat.opacity = 0.7 + 0.3 * (0.5 + 0.5 * Math.sin(t * 4));
      }
      updateLabels();
      controls.update();
      renderer.render(scene, camera);
    }
    animate();

    stateRef.current = {
      classes, episodes, dotPositions, selectByGlobalIdx, deselect, setLegendHighlight,
      focusToward, seeds,
      get pinnedLegendClass() { return pinnedLegendClass; },
      set pinnedLegendClass(v) { pinnedLegendClass = v; lastInteractionTime = performance.now(); },
      selectedGlobalIdxRef: () => selectedGlobalIdx,
    };

    return () => {
      cancelAnimationFrame(raf);
      resizeObserver.disconnect();
      window.removeEventListener("scroll", updateCanvasRect, { capture: true });
      renderer.domElement.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointerup", onPointerUpGlobal);
      window.removeEventListener("pointercancel", onPointerUpGlobal);
      renderer.domElement.removeEventListener("pointerup", onCanvasPointerUp);
      renderer.domElement.removeEventListener("pointermove", onCanvasPointerMove);
      controls.dispose();
      // renderer.dispose() alone does NOT free the geometries/materials this
      // effect created -- without this, every rebuild (e.g. remounting on a
      // route change) leaked VRAM that renderer.dispose() never touches,
      // degrading the whole GPU process, not just this tab, the more times
      // the scene got rebuilt.
      scene.traverse((obj) => {
        obj.geometry?.dispose();
        const mats = Array.isArray(obj.material) ? obj.material : obj.material ? [obj.material] : [];
        mats.forEach((m) => m.dispose());
      });
      renderer.dispose();
      container.removeChild(renderer.domElement);
      container.removeChild(labelContainer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodes]);

  useEffect(() => {
    const s = stateRef.current;
    if (!s.episodes) return;
    if (!selectedId) { s.deselect?.(); return; }
    const idx = s.episodes.findIndex((e) => e.episode_id === selectedId);
    if (idx >= 0 && s.selectedGlobalIdxRef?.() !== idx) s.selectByGlobalIdx(idx);
  }, [selectedId]);

  useEffect(() => {
    const s = stateRef.current;
    if (!s.classes) return;
    const ci = filterClass ? s.classes.indexOf(filterClass) : -1;
    s.pinnedLegendClass = ci >= 0 ? ci : null;
    s.setLegendHighlight?.(ci >= 0 ? ci : null);
    if (ci >= 0) s.focusToward?.(s.seeds[ci].clone());
  }, [filterClass]);

  const classes = [...new Set(episodes.map((e) => e.fault_class))].sort();

  return (
    <div className="relative overflow-hidden border border-outline-variant bg-surface-container-lowest" style={{ height }}>
      {hudVisible && (
        <div className="absolute top-0 left-0 right-0 z-10 flex justify-between items-center px-3 py-2 bg-surface-container-low/80 border-b border-outline-variant/50">
          <span className="font-label-caps text-[10px] text-primary tracking-widest">EPISODE_TOPOLOGY</span>
          <span className="font-data-mono text-[10px] text-on-surface-variant">{episodes.length} episodes</span>
        </div>
      )}
      <button
        type="button"
        onClick={() => setHudVisible((v) => !v)}
        className="absolute top-9 right-2 z-10 bg-surface-container-low/80 border border-outline-variant/50 text-on-surface-variant font-label-caps text-[10px] tracking-widest px-3 py-1.5 hover:bg-surface-variant/50"
      >
        {hudVisible ? "HIDE HUD" : "SHOW HUD"}
      </button>
      <div ref={containerRef} className="w-full h-full" />
      {hudVisible && (
      <div className="absolute bottom-2 left-2 z-10 bg-surface/80 backdrop-blur border border-outline-variant/50 p-2 max-h-[45%] overflow-y-auto text-[10px] font-data-mono">
        {classes.map((c) => {
          const count = episodes.filter((e) => e.fault_class === c).length;
          const active = filterClass === c;
          return (
            <div
              key={c}
              onClick={() => onFilterClass?.(active ? null : c)}
              className={`flex items-center gap-1.5 px-1.5 py-0.5 rounded-sm cursor-pointer hover:bg-surface-variant/50 ${active ? "bg-surface-variant text-primary" : "text-on-surface-variant"}`}
            >
              <span className="w-2 h-2 rounded-full shrink-0" style={{ background: hexToCss(colorForClass(c)) }} />
              {c} ({count})
            </div>
          );
        })}
      </div>
      )}
    </div>
  );
}
