/* Building Modeller frontend: talks to the Flask REST API and renders
 * footprints / LiDAR point cloud / the live, directly-editable building
 * mesh with Three.js.
 *
 * There is no roof-type picker: every building starts as a flat box and
 * is shaped by selecting and moving individual vertices (click to select,
 * click-and-drag vertically to push/pull, or type exact coordinates) --
 * see model/mesh.py for why.
 *
 * All coordinates coming from the API are already relative to the
 * server-side scene origin (see meshutil.py) -- this file never has to
 * deal with raw RD New coordinates.
 */
(function () {
  "use strict";

  const els = {
    areaForm: document.getElementById("area-form"),
    bboxMinX: document.getElementById("bbox-minx"),
    bboxMinY: document.getElementById("bbox-miny"),
    bboxMaxX: document.getElementById("bbox-maxx"),
    bboxMaxY: document.getElementById("bbox-maxy"),
    lidarFolder: document.getElementById("lidar-folder"),
    warnings: document.getElementById("warnings"),
    buildingList: document.getElementById("building-list"),
    viewportHint: document.getElementById("viewport-hint"),
    buildingPanelEmpty: document.getElementById("building-panel-empty"),
    buildingInfo: document.getElementById("building-info"),
    infoBagId: document.getElementById("info-bag-id"),
    infoLidarStats: document.getElementById("info-lidar-stats"),
    vertexPanelEmpty: document.getElementById("vertex-panel-empty"),
    vertexForm: document.getElementById("vertex-form"),
    vertexIndex: document.getElementById("vertex-index"),
    vertexSurface: document.getElementById("vertex-surface"),
    vertexX: document.getElementById("vertex-x"),
    vertexY: document.getElementById("vertex-y"),
    vertexZ: document.getElementById("vertex-z"),
    btnSnapLidar: document.getElementById("btn-snap-lidar"),
    btnSaveSession: document.getElementById("btn-save-session"),
    btnLoadSession: document.getElementById("btn-load-session"),
    loadSessionFile: document.getElementById("load-session-file"),
    btnExport: document.getElementById("btn-export"),
  };

  const state = {
    buildings: [],
    selectedBagId: null,
    meshData: null, // last-fetched {vertices, triangles, faces} for the selected building
    selectedVertexIndex: null,
    updatingPanel: false,
  };

  // ---- API helpers --------------------------------------------------

  async function apiGetJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  }

  async function apiPostJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  }

  function downloadBlobResponse(res, fallbackName) {
    return res.blob().then((blob) => {
      const disposition = res.headers.get("Content-Disposition") || "";
      const match = /filename=([^;]+)/.exec(disposition);
      const filename = match ? match[1].trim() : fallbackName;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    });
  }

  // ---- Three.js scene -------------------------------------------------

  const canvas = document.getElementById("viewport");
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0e1620);

  const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 10000);
  camera.position.set(80, -120, 90);
  camera.up.set(0, 0, 1);

  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 0, 0);
  controls.enableDamping = true;

  scene.add(new THREE.AmbientLight(0xffffff, 0.6));
  const sun = new THREE.DirectionalLight(0xffffff, 0.8);
  sun.position.set(1, -1, 2);
  scene.add(sun);

  const grid = new THREE.GridHelper(400, 20, 0x2a3a4a, 0x1c2a38);
  grid.rotation.x = Math.PI / 2; // GridHelper is XZ by default; our scene uses Z-up.
  scene.add(grid);

  let footprintGroup = new THREE.Group();
  scene.add(footprintGroup);
  let pointCloudObject = null;
  let buildingMeshObject = null;
  let vertexMarkerGroup = new THREE.Group();
  scene.add(vertexMarkerGroup);

  const VERTEX_COLOR = 0xffe066;
  const VERTEX_SELECTED_COLOR = 0x33e0ff;
  const vertexGeometry = new THREE.SphereGeometry(0.25, 10, 8);

  function resizeRenderer() {
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
  }
  window.addEventListener("resize", resizeRenderer);

  function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }
  resizeRenderer();
  animate();

  // ---- Rendering helpers ------------------------------------------------

  function clearFootprints() {
    scene.remove(footprintGroup);
    footprintGroup = new THREE.Group();
    scene.add(footprintGroup);
  }

  function addFootprintOutline(ring, z, color) {
    const points = ring.map(([x, y]) => new THREE.Vector3(x, y, z));
    points.push(points[0]);
    const geometry = new THREE.BufferGeometry().setFromPoints(points);
    const material = new THREE.LineBasicMaterial({ color });
    footprintGroup.add(new THREE.Line(geometry, material));
  }

  function renderFootprints(buildings) {
    clearFootprints();
    for (const b of buildings) {
      const color = b.status === "unmodelled" ? 0x3d72b4 : 0x4fd18b;
      addFootprintOutline(b.footprint, b.ground_height || 0, color);
    }
  }

  function elevationColor(t) {
    // Simple low->high ramp: blue-ish at low t, red-ish at high t.
    return new THREE.Color(t, 0.3, 1.0 - t);
  }

  function renderPointCloud(pc) {
    if (pointCloudObject) {
      scene.remove(pointCloudObject);
      pointCloudObject.geometry.dispose();
      pointCloudObject.material.dispose();
      pointCloudObject = null;
    }
    const n = pc.x.length;
    if (n === 0) return;

    let zmin = Infinity, zmax = -Infinity;
    for (let i = 0; i < n; i++) {
      if (pc.z[i] < zmin) zmin = pc.z[i];
      if (pc.z[i] > zmax) zmax = pc.z[i];
    }
    const span = Math.max(zmax - zmin, 1e-6);

    const positions = new Float32Array(n * 3);
    const colors = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      positions[i * 3] = pc.x[i];
      positions[i * 3 + 1] = pc.y[i];
      positions[i * 3 + 2] = pc.z[i];
      const t = (pc.z[i] - zmin) / span;
      const c = elevationColor(t);
      colors[i * 3] = c.r;
      colors[i * 3 + 1] = c.g;
      colors[i * 3 + 2] = c.b;
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    const material = new THREE.PointsMaterial({ size: 0.6, vertexColors: true });
    pointCloudObject = new THREE.Points(geometry, material);
    scene.add(pointCloudObject);
  }

  function renderBuildingMesh(meshData) {
    if (buildingMeshObject) {
      scene.remove(buildingMeshObject);
      buildingMeshObject.geometry.dispose();
      buildingMeshObject.material.dispose();
      buildingMeshObject = null;
    }
    if (!meshData || meshData.vertices.length === 0) return;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute(
      "position",
      new THREE.BufferAttribute(new Float32Array(meshData.vertices), 3)
    );
    geometry.setIndex(meshData.triangles);
    geometry.computeVertexNormals();
    const material = new THREE.MeshStandardMaterial({
      color: 0xd9522e,
      side: THREE.DoubleSide,
      flatShading: true,
    });
    buildingMeshObject = new THREE.Mesh(geometry, material);
    scene.add(buildingMeshObject);
  }

  function clearVertexMarkers() {
    scene.remove(vertexMarkerGroup);
    vertexMarkerGroup = new THREE.Group();
    scene.add(vertexMarkerGroup);
  }

  function renderVertexMarkers(meshData) {
    clearVertexMarkers();
    if (!meshData) return;
    const n = meshData.vertices.length / 3;
    for (let i = 0; i < n; i++) {
      const material = new THREE.MeshBasicMaterial({
        color: i === state.selectedVertexIndex ? VERTEX_SELECTED_COLOR : VERTEX_COLOR,
      });
      const marker = new THREE.Mesh(vertexGeometry, material);
      marker.position.set(
        meshData.vertices[i * 3],
        meshData.vertices[i * 3 + 1],
        meshData.vertices[i * 3 + 2]
      );
      marker.userData.vertexIndex = i;
      vertexMarkerGroup.add(marker);
    }
  }

  function setVertexPosition(meshData, index, x, y, z) {
    meshData.vertices[index * 3] = x;
    meshData.vertices[index * 3 + 1] = y;
    meshData.vertices[index * 3 + 2] = z;
  }

  // ---- Warnings ----------------------------------------------------------

  function showWarnings(warnings) {
    els.warnings.innerHTML = "";
    for (const w of warnings || []) {
      const div = document.createElement("div");
      div.className = "warning";
      div.textContent = w;
      els.warnings.appendChild(div);
    }
  }

  // ---- Shared "apply a buildings list to the whole UI" ------------------
  // Used after loading an area, uploading a session, and restoring the
  // autosaved session on page load.

  function applyBuildingsResult(buildings, options) {
    options = options || {};
    state.buildings = buildings;
    state.selectedBagId = null;
    state.meshData = null;
    state.selectedVertexIndex = null;
    renderFootprints(state.buildings);
    renderPointCloud(options.pointCloud || { x: [], y: [], z: [] });
    renderBuildingMesh(null);
    clearVertexMarkers();
    renderBuildingList();
    els.buildingPanelEmpty.hidden = false;
    els.buildingInfo.hidden = true;
    deselectVertex();
    els.viewportHint.textContent = options.hint || "";
  }

  // ---- Building list -------------------------------------------------

  function renderBuildingList() {
    els.buildingList.innerHTML = "";
    for (const b of state.buildings) {
      const li = document.createElement("li");
      if (b.bag_id === state.selectedBagId) li.classList.add("selected");
      const label = document.createElement("span");
      label.textContent = b.bag_id;
      const status = document.createElement("span");
      status.className = "status-tag " + b.status;
      status.textContent = b.status;
      li.appendChild(label);
      li.appendChild(status);
      li.addEventListener("click", () => selectBuilding(b.bag_id));
      els.buildingList.appendChild(li);
    }
  }

  async function selectBuilding(bagId) {
    state.selectedBagId = bagId;
    state.selectedVertexIndex = null;
    renderBuildingList();
    const building = state.buildings.find((b) => b.bag_id === bagId);
    if (!building) return;

    els.buildingPanelEmpty.hidden = true;
    els.buildingInfo.hidden = false;
    els.infoBagId.textContent = building.bag_id;
    const stats = building.lidar_stats;
    els.infoLidarStats.textContent = stats
      ? `${stats.point_count} pts, ground ${stats.ground_height.toFixed(2)} m, top ${stats.top_height.toFixed(2)} m`
      : "no LiDAR stats";

    const meshData = await apiGetJSON(`/api/buildings/${encodeURIComponent(bagId)}/mesh`);
    state.meshData = meshData;
    renderBuildingMesh(meshData);
    deselectVertex(); // also renders the (unselected) vertex markers
  }

  // ---- Vertex selection, editing, and dragging ---------------------------

  function surfaceTypesForVertex(meshData, index) {
    const types = new Set();
    for (const f of meshData.faces) {
      if (f.indices.includes(index)) types.add(f.surface_type);
    }
    return Array.from(types).join(", ");
  }

  function selectVertex(index) {
    state.selectedVertexIndex = index;
    renderVertexMarkers(state.meshData);
    fillVertexForm();
  }

  function deselectVertex() {
    state.selectedVertexIndex = null;
    els.vertexPanelEmpty.hidden = false;
    els.vertexForm.hidden = true;
    if (state.meshData) renderVertexMarkers(state.meshData);
  }

  function fillVertexForm() {
    if (state.selectedVertexIndex === null || !state.meshData) return;
    state.updatingPanel = true;
    try {
      els.vertexPanelEmpty.hidden = true;
      els.vertexForm.hidden = false;
      const i = state.selectedVertexIndex;
      els.vertexIndex.textContent = i;
      els.vertexSurface.textContent = surfaceTypesForVertex(state.meshData, i);
      els.vertexX.value = state.meshData.vertices[i * 3].toFixed(2);
      els.vertexY.value = state.meshData.vertices[i * 3 + 1].toFixed(2);
      els.vertexZ.value = state.meshData.vertices[i * 3 + 2].toFixed(2);
    } finally {
      state.updatingPanel = false;
    }
  }

  async function submitVertexForm() {
    if (state.updatingPanel || state.selectedVertexIndex === null || !state.selectedBagId) return;
    const x = parseFloat(els.vertexX.value);
    const y = parseFloat(els.vertexY.value);
    const z = parseFloat(els.vertexZ.value);
    await moveSelectedVertex(x, y, z);
  }

  async function moveSelectedVertex(x, y, z) {
    const bagId = state.selectedBagId;
    const index = state.selectedVertexIndex;
    const result = await apiPostJSON(
      `/api/buildings/${encodeURIComponent(bagId)}/vertex/${index}`,
      { x, y, z }
    );
    applyMeshUpdate(bagId, result);
  }

  function applyMeshUpdate(bagId, result) {
    const idx = state.buildings.findIndex((b) => b.bag_id === bagId);
    if (idx >= 0) state.buildings[idx] = result.building;
    renderBuildingList();
    state.meshData = result.mesh;
    renderBuildingMesh(result.mesh);
    renderVertexMarkers(result.mesh);
    fillVertexForm();
  }

  els.vertexX.addEventListener("change", submitVertexForm);
  els.vertexY.addEventListener("change", submitVertexForm);
  els.vertexZ.addEventListener("change", submitVertexForm);

  els.btnSnapLidar.addEventListener("click", async () => {
    if (state.selectedVertexIndex === null || !state.selectedBagId) return;
    try {
      const result = await apiPostJSON(
        `/api/buildings/${encodeURIComponent(state.selectedBagId)}/vertex/${state.selectedVertexIndex}/snap_lidar`
      );
      applyMeshUpdate(state.selectedBagId, result);
      showWarnings([]);
    } catch (err) {
      showWarnings([`Snap to LiDAR failed: ${err.message}`]);
    }
  });

  // ---- Vertex picking + vertical drag on the canvas ----------------------

  const raycaster = new THREE.Raycaster();
  const pointerNDC = new THREE.Vector2();
  let dragState = null; // { index, startClientY, startZ, moved }
  const DRAG_THRESHOLD_PX = 3;

  function pickVertexMarker(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    pointerNDC.x = ((clientX - rect.left) / rect.width) * 2 - 1;
    pointerNDC.y = -((clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointerNDC, camera);
    const hits = raycaster.intersectObjects(vertexMarkerGroup.children);
    return hits.length > 0 ? hits[0].object.userData.vertexIndex : null;
  }

  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const index = pickVertexMarker(event.clientX, event.clientY);
    if (index === null) return;
    // Disable orbiting immediately -- OrbitControls checks this flag at
    // the start of its own pointerdown handling on the same canvas, so
    // this stops it from also starting a camera-rotate on this event.
    controls.enabled = false;
    selectVertex(index);
    dragState = {
      index,
      startClientY: event.clientY,
      startZ: state.meshData.vertices[index * 3 + 2],
      moved: false,
    };
    canvas.setPointerCapture(event.pointerId);
  });

  canvas.addEventListener("pointermove", (event) => {
    if (!dragState) return;
    const deltaY = dragState.startClientY - event.clientY;
    if (!dragState.moved && Math.abs(deltaY) < DRAG_THRESHOLD_PX) return;
    dragState.moved = true;

    const distance = camera.position.distanceTo(controls.target);
    const sensitivity = Math.max(distance * 0.002, 0.005);
    const newZ = dragState.startZ + deltaY * sensitivity;

    const i = dragState.index;
    setVertexPosition(state.meshData, i, state.meshData.vertices[i * 3], state.meshData.vertices[i * 3 + 1], newZ);
    renderBuildingMesh(state.meshData);
    renderVertexMarkers(state.meshData);
    if (state.selectedVertexIndex === i) fillVertexForm();
  });

  async function endDrag(event) {
    if (!dragState) return;
    controls.enabled = true;
    canvas.releasePointerCapture(event.pointerId);
    const wasDrag = dragState.moved;
    const index = dragState.index;
    dragState = null;
    if (!wasDrag) return; // a plain click just selects, nothing to persist
    const i = index;
    try {
      await moveSelectedVertex(
        state.meshData.vertices[i * 3],
        state.meshData.vertices[i * 3 + 1],
        state.meshData.vertices[i * 3 + 2]
      );
    } catch (err) {
      showWarnings([`Failed to move vertex: ${err.message}`]);
    }
  }

  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);

  // ---- Area loading -------------------------------------------------

  els.areaForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const bbox = [
      parseFloat(els.bboxMinX.value),
      parseFloat(els.bboxMinY.value),
      parseFloat(els.bboxMaxX.value),
      parseFloat(els.bboxMaxY.value),
    ];
    const lidarFolder = els.lidarFolder.value;

    els.viewportHint.textContent = "Loading...";
    try {
      const result = await apiPostJSON("/api/area", { bbox, lidar_folder: lidarFolder });
      applyBuildingsResult(result.buildings, {
        pointCloud: result.point_cloud,
        hint: result.buildings.length ? "" : "No buildings found for this area.",
      });
      showWarnings(result.warnings);
      if (result.point_cloud.total_points > result.point_cloud.shown_points) {
        showWarnings([
          ...(result.warnings || []),
          `Point cloud decimated for display: showing ${result.point_cloud.shown_points} of ${result.point_cloud.total_points} points.`,
        ]);
      }
    } catch (err) {
      els.viewportHint.textContent = "";
      showWarnings([`Failed to load area: ${err.message}`]);
    }
  });

  // ---- Session save/load, export -------------------------------------

  els.btnSaveSession.addEventListener("click", async () => {
    const res = await fetch("/api/session");
    if (!res.ok) {
      showWarnings([await res.text()]);
      return;
    }
    await downloadBlobResponse(res, "session.json");
  });

  els.btnLoadSession.addEventListener("click", () => els.loadSessionFile.click());

  els.loadSessionFile.addEventListener("change", async () => {
    const file = els.loadSessionFile.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append("file", file);
    try {
      const res = await fetch("/api/session", { method: "POST", body: formData });
      if (!res.ok) throw new Error(await res.text());
      const result = await res.json();
      applyBuildingsResult(result.buildings);
      showWarnings([]);
    } catch (err) {
      showWarnings([`Failed to load session: ${err.message}`]);
    } finally {
      els.loadSessionFile.value = "";
    }
  });

  els.btnExport.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/export", { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      await downloadBlobResponse(res, "buildings.gml");
      renderBuildingList();
    } catch (err) {
      showWarnings([`Export failed: ${err.message}`]);
    }
  });

  // ---- Restore the autosaved session on page load ------------------

  (async function bootstrap() {
    try {
      const result = await apiGetJSON("/api/buildings");
      if (result.buildings && result.buildings.length > 0) {
        applyBuildingsResult(result.buildings);
        showWarnings([
          `Restored previous session (${result.buildings.length} building(s)). ` +
            "Reload the area to bring back the LiDAR point cloud.",
        ]);
      }
    } catch (err) {
      console.warn("Could not restore autosaved session:", err);
    }
  })();
})();
