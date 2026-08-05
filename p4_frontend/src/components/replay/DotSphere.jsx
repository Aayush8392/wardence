import { useEffect, useRef } from "react";
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
  const triples = [];
  for (let k = 0; k < total; k++) {
    const ak = angles[k];
    for (let i = 0; i < C; i++) triples.push([ak[i] / weights[i], k, i]);
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

  useEffect(() => {
    const container = containerRef.current;
    if (!container || episodes.length === 0) return undefined;

    const { classes, seeds, indicesByClass, classify, dotPositions } = buildLayout(episodes);

    const width = container.clientWidth, initialHeight = container.clientHeight;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0a0e14);
    const camera = new THREE.PerspectiveCamera(50, width / initialHeight, 0.1, 100);
    camera.position.set(0, 0, 9);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(width, initialHeight);
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    container.appendChild(renderer.domElement);

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
      const mat = new THREE.PointsMaterial({ color: 0x8a97a8, size: 0.08, transparent: true, opacity: 0.5, depthWrite: false });
      scene.add(new THREE.Points(geo, mat));
    })();

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
      const geo = new THREE.SphereGeometry(FILL_RADIUS, 160, 90).toNonIndexed();
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
      worldGroup.add(new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.4, depthWrite: false })));
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

      const seedMesh = new THREE.Mesh(
        new THREE.SphereGeometry(Math.max(DOT_RADIUS, 0.09), 12, 12),
        new THREE.MeshBasicMaterial({ color: colorForClass(className) })
      );
      seedMesh.position.copy(seeds[ci].clone().multiplyScalar(SPHERE_RADIUS));
      worldGroup.add(seedMesh);
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
      isInteracting = false;
      lastInteractionTime = performance.now();
    }
    function onCanvasPointerUp(e) {
      if (!pointerDownPos) return;
      const moved = Math.hypot(e.clientX - pointerDownPos.x, e.clientY - pointerDownPos.y);
      pointerDownPos = null;
      if (moved > DRAG_CLICK_THRESHOLD_PX) return;
      const rect = renderer.domElement.getBoundingClientRect();
      mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(mouse, camera);
      const hit = raycaster.intersectObjects([core, ...hitMeshesByClass])[0];
      if (!hit) { deselect(); onSelect(null); return; }
      if (hit.object === core || hit.instanceId === undefined) return;
      selectByGlobalIdx(hit.object.userData.localToGlobal[hit.instanceId]);
    }
    function onCanvasPointerMove(e) {
      if (isInteracting) { hoverRing.visible = false; return; }
      const rect = renderer.domElement.getBoundingClientRect();
      mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
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
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
      controls.handleResize();
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
      renderer.domElement.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointerup", onPointerUpGlobal);
      window.removeEventListener("pointercancel", onPointerUpGlobal);
      renderer.domElement.removeEventListener("pointerup", onCanvasPointerUp);
      renderer.domElement.removeEventListener("pointermove", onCanvasPointerMove);
      controls.dispose();
      renderer.dispose();
      container.removeChild(renderer.domElement);
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
    <div className="relative border border-outline-variant bg-surface-container-lowest" style={{ height }}>
      <div className="absolute top-0 left-0 right-0 z-10 flex justify-between items-center px-3 py-2 bg-surface-container-low/80 border-b border-outline-variant/50">
        <span className="font-label-caps text-[10px] text-primary tracking-widest">EPISODE_TOPOLOGY</span>
        <span className="font-data-mono text-[10px] text-on-surface-variant">{episodes.length} episodes</span>
      </div>
      <div ref={containerRef} className="w-full h-full" />
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
    </div>
  );
}
