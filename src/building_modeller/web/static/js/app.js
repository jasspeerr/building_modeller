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
    btnDeleteVertex: document.getElementById("btn-delete-vertex"),
    edgePanel: document.getElementById("edge-panel"),
    edgeA: document.getElementById("edge-a"),
    edgeB: document.getElementById("edge-b"),
    edgeNotAdjacent: document.getElementById("edge-not-adjacent"),
    btnSplitEdge: document.getElementById("btn-split-edge"),
    btnSplitFace: document.getElementById("btn-split-face"),
    extrudeDistanceLabel: document.getElementById("extrude-distance-label"),
    extrudeDistance: document.getElementById("extrude-distance"),
    btnExtrudeFace: document.getElementById("btn-extrude-face"),
    btnSaveSession: document.getElementById("btn-save-session"),
    btnLoadSession: document.getElementById("btn-load-session"),
    loadSessionFile: document.getElementById("load-session-file"),
    btnExport: document.getElementById("btn-export"),
    areaSubmit: document.querySelector("#area-form button[type=submit]"),
    loadingOverlay: document.getElementById("loading-overlay"),
    loadingPhase: document.getElementById("loading-phase"),
    loadingFill: document.getElementById("loading-fill"),
    loadingMessage: document.getElementById("loading-message"),
    btnCancelLoad: document.getElementById("btn-cancel-load"),
    pcColorMode: document.getElementById("pc-color-mode"),
    pcHideVegetation: document.getElementById("pc-hide-vegetation"),
    pcLegend: document.getElementById("pc-legend"),
  };

  const state = {
    buildings: [],
    selectedBagId: null,
    meshData: null, // last-fetched {vertices, triangles, faces} for the selected building
    selectedVertexIndex: null,
    secondaryVertexIndex: null, // shift+click target, for edge selection
    updatingPanel: false,
    pointCloud: null, // last-fetched payload, kept so re-colouring needs no refetch
    jobId: null,
    jobProgress: 0, // client-side monotonic guard: polls can land out of order
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
  let terrainObject = null;
  let vertexMarkerGroup = new THREE.Group();
  scene.add(vertexMarkerGroup);

  const VERTEX_COLOR = 0xffe066;
  const VERTEX_SELECTED_COLOR = 0x33e0ff;
  const VERTEX_SECONDARY_COLOR = 0xb066ff;
  const vertexGeometry = new THREE.SphereGeometry(0.25, 10, 8);

  // ASPRS classes, matching data/pointcloud.py's constants.
  const CLASS_COLORS = {
    2: 0x8a7a5c, // ground
    6: 0xd9522e, // building
    3: 0x3f8f4f, // low vegetation
    4: 0x4fae5f, // medium vegetation
    5: 0x5fd070, // high vegetation
    9: 0x2f7fc0, // water
    7: 0x606870, // noise
    18: 0x606870, // high noise
  };
  const CLASS_LABELS = {
    2: "ground",
    6: "building",
    5: "vegetation",
    9: "water",
    7: "noise",
  };
  const CLASS_OTHER_COLOR = 0x9aa6b2;

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

  function classColor(code) {
    const hex = CLASS_COLORS[code];
    return new THREE.Color(hex === undefined ? CLASS_OTHER_COLOR : hex);
  }

  function renderPointCloud(pc) {
    state.pointCloud = pc;
    if (pointCloudObject) {
      scene.remove(pointCloudObject);
      pointCloudObject.geometry.dispose();
      pointCloudObject.material.dispose();
      pointCloudObject = null;
    }
    renderPointCloudLegend();
    const n = pc.x.length;
    if (n === 0) return;

    const byClass = els.pcColorMode.value === "classification" && pc.classification;
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
      const c = byClass
        ? classColor(pc.classification[i])
        : elevationColor((pc.z[i] - zmin) / span);
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

  function renderPointCloudLegend() {
    els.pcLegend.innerHTML = "";
    if (els.pcColorMode.value !== "classification") return;
    for (const code of Object.keys(CLASS_LABELS)) {
      const row = document.createElement("div");
      row.className = "legend-row";
      const swatch = document.createElement("span");
      swatch.className = "legend-swatch";
      swatch.style.background = "#" + classColor(Number(code)).getHexString();
      const label = document.createElement("span");
      label.textContent = CLASS_LABELS[code];
      row.appendChild(swatch);
      row.appendChild(label);
      els.pcLegend.appendChild(row);
    }
  }

  function renderTerrain(data) {
    if (terrainObject) {
      scene.remove(terrainObject);
      terrainObject.geometry.dispose();
      terrainObject.material.dispose();
      terrainObject = null;
    }
    if (!data || !data.vertices || data.vertices.length === 0) return;
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute(
      "position",
      new THREE.BufferAttribute(new Float32Array(data.vertices), 3)
    );
    geometry.setIndex(data.triangles);
    geometry.computeVertexNormals();
    const material = new THREE.MeshStandardMaterial({
      color: 0x5b6b57,
      side: THREE.DoubleSide,
      flatShading: true,
      // Each building's footprint cells are stamped with that building's
      // own ground height, so the terrain there is exactly coplanar with
      // its GroundSurface. Nudge the terrain back in the depth buffer so
      // the pair doesn't z-fight.
      polygonOffset: true,
      polygonOffsetFactor: 1,
      polygonOffsetUnits: 1,
    });
    terrainObject = new THREE.Mesh(geometry, material);
    scene.add(terrainObject);
  }

  async function refreshTerrain() {
    try {
      renderTerrain(await apiGetJSON("/api/terrain"));
    } catch (err) {
      console.warn("Could not load terrain:", err);
    }
  }

  async function refreshPointCloud() {
    const query = els.pcHideVegetation.checked ? "?hide_vegetation=1" : "";
    try {
      renderPointCloud(await apiGetJSON("/api/pointcloud" + query));
    } catch (err) {
      showWarnings([`Could not load point cloud: ${err.message}`]);
    }
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

  function colorForVertex(i) {
    if (i === state.selectedVertexIndex) return VERTEX_SELECTED_COLOR;
    if (i === state.secondaryVertexIndex) return VERTEX_SECONDARY_COLOR;
    return VERTEX_COLOR;
  }

  function renderVertexMarkers(meshData) {
    clearVertexMarkers();
    if (!meshData) return;
    const n = meshData.vertices.length / 3;
    for (let i = 0; i < n; i++) {
      const material = new THREE.MeshBasicMaterial({ color: colorForVertex(i) });
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
    renderPointCloud(options.pointCloud || { x: [], y: [], z: [], classification: [] });
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
    state.secondaryVertexIndex = null;
    renderVertexMarkers(state.meshData);
    fillVertexForm();
    updateEdgePanel();
  }

  function selectSecondaryVertex(index) {
    state.secondaryVertexIndex = index;
    renderVertexMarkers(state.meshData);
    updateEdgePanel();
  }

  function deselectVertex() {
    state.selectedVertexIndex = null;
    state.secondaryVertexIndex = null;
    els.vertexPanelEmpty.hidden = false;
    els.vertexForm.hidden = true;
    if (state.meshData) renderVertexMarkers(state.meshData);
    updateEdgePanel();
  }

  function isAdjacentInFace(idx, a, b) {
    const n = idx.length;
    for (let i = 0; i < n; i++) {
      const pair = new Set([idx[i], idx[(i + 1) % n]]);
      if (pair.has(a) && pair.has(b) && pair.size === 2) return true;
    }
    return false;
  }

  function edgeIsAdjacent(meshData, a, b) {
    return meshData.faces.some((f) => isAdjacentInFace(f.indices, a, b));
  }

  // The single face (if any) that has both a and b as a *non-adjacent*
  // diagonal -- mirrors EditableMesh._face_for_diagonal server-side,
  // used here only to decide which buttons to show.
  function uniqueDiagonalFace(meshData, a, b) {
    const candidates = meshData.faces.filter(
      (f) => f.indices.includes(a) && f.indices.includes(b) && !isAdjacentInFace(f.indices, a, b)
    );
    return candidates.length === 1 ? candidates[0] : null;
  }

  function updateEdgePanel() {
    const a = state.selectedVertexIndex;
    const b = state.secondaryVertexIndex;
    if (a === null || b === null || !state.meshData) {
      els.edgePanel.hidden = true;
      return;
    }
    els.edgePanel.hidden = false;
    els.edgeA.textContent = a;
    els.edgeB.textContent = b;

    const adjacent = edgeIsAdjacent(state.meshData, a, b);
    const diagonalFace = uniqueDiagonalFace(state.meshData, a, b);

    els.btnSplitEdge.hidden = !adjacent;
    els.btnSplitFace.hidden = !diagonalFace;
    els.extrudeDistanceLabel.hidden = !diagonalFace;
    els.btnExtrudeFace.hidden = !diagonalFace;
    els.edgeNotAdjacent.hidden = adjacent || !!diagonalFace;
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
    updateEdgePanel();
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

  els.btnDeleteVertex.addEventListener("click", async () => {
    if (state.selectedVertexIndex === null || !state.selectedBagId) return;
    const bagId = state.selectedBagId;
    const index = state.selectedVertexIndex;
    try {
      const result = await apiPostJSON(
        `/api/buildings/${encodeURIComponent(bagId)}/vertex/${index}/delete`
      );
      // Deleting reindexes every vertex after it, so the previous
      // selection is no longer meaningful -- deselect rather than guess.
      state.selectedVertexIndex = null;
      state.secondaryVertexIndex = null;
      applyMeshUpdate(bagId, result);
      deselectVertex();
      showWarnings([]);
    } catch (err) {
      showWarnings([`Failed to delete vertex: ${err.message}`]);
    }
  });

  els.btnSplitEdge.addEventListener("click", async () => {
    const a = state.selectedVertexIndex;
    const b = state.secondaryVertexIndex;
    if (a === null || b === null || !state.selectedBagId) return;
    const bagId = state.selectedBagId;
    try {
      const result = await apiPostJSON(`/api/buildings/${encodeURIComponent(bagId)}/edge/split`, {
        index_a: a,
        index_b: b,
      });
      applyMeshUpdate(bagId, result);
      selectVertex(result.new_vertex_index); // jump straight to the new vertex
      showWarnings([]);
    } catch (err) {
      showWarnings([`Failed to split edge: ${err.message}`]);
    }
  });

  els.btnSplitFace.addEventListener("click", async () => {
    const a = state.selectedVertexIndex;
    const b = state.secondaryVertexIndex;
    if (a === null || b === null || !state.selectedBagId) return;
    const bagId = state.selectedBagId;
    try {
      const result = await apiPostJSON(`/api/buildings/${encodeURIComponent(bagId)}/face/split`, {
        index_a: a,
        index_b: b,
      });
      applyMeshUpdate(bagId, result);
      deselectVertex(); // the diagonal no longer identifies a single face
      showWarnings([]);
    } catch (err) {
      showWarnings([`Failed to split face: ${err.message}`]);
    }
  });

  els.btnExtrudeFace.addEventListener("click", async () => {
    const a = state.selectedVertexIndex;
    const b = state.secondaryVertexIndex;
    if (a === null || b === null || !state.selectedBagId) return;
    const distance = parseFloat(els.extrudeDistance.value);
    if (!Number.isFinite(distance) || distance === 0) {
      showWarnings(["Extrude distance must be a non-zero number."]);
      return;
    }
    const bagId = state.selectedBagId;
    try {
      const result = await apiPostJSON(`/api/buildings/${encodeURIComponent(bagId)}/face/extrude`, {
        index_a: a,
        index_b: b,
        distance,
      });
      applyMeshUpdate(bagId, result);
      deselectVertex(); // a/b are now skirt-boundary vertices, not a face diagonal
      showWarnings([]);
    } catch (err) {
      showWarnings([`Failed to extrude face: ${err.message}`]);
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

    if (event.shiftKey && state.selectedVertexIndex !== null && index !== state.selectedVertexIndex) {
      // Shift+click a second, different vertex: pick an edge, don't drag.
      // No dragState is set here, so endDrag() never runs for this event
      // -- re-enable orbiting immediately rather than leaving it stuck off.
      selectSecondaryVertex(index);
      controls.enabled = true;
      return;
    }

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

    try {
      const started = await apiPostJSON("/api/area", { bbox, lidar_folder: lidarFolder });
      state.jobId = started.job_id;
      state.jobProgress = 0;
      setBusy(true);
      showOverlay("Starting", 0, false, "");
      pollAreaJob();
    } catch (err) {
      setBusy(false);
      hideOverlay();
      showWarnings([`Failed to start area load: ${err.message}`]);
    }
  });

  function pollAreaJob() {
    setTimeout(async () => {
      // Re-armed after each response rather than setInterval, so a slow
      // response can't let polls stack up.
      if (!state.jobId) return;
      let job;
      try {
        job = await apiGetJSON(`/api/area/jobs/${state.jobId}`);
      } catch (err) {
        // 404 means the job is gone (server restart, or evicted from the
        // history): stop polling and re-sync rather than hanging forever.
        finishAreaJob();
        showWarnings([`Lost track of the area load: ${err.message}`]);
        await resyncFromServer();
        return;
      }
      updateOverlay(job);
      if (job.state === "running") {
        pollAreaJob();
        return;
      }
      finishAreaJob();
      if (job.state === "error") {
        showWarnings([`Area load failed: ${job.error}`]);
        return;
      }
      if (job.state === "cancelled") {
        showWarnings(["Area load cancelled."]);
        return;
      }
      await applyFinishedLoad(job);
    }, 300);
  }

  async function applyFinishedLoad(job) {
    const listing = await apiGetJSON("/api/buildings");
    applyBuildingsResult(listing.buildings, {
      hint: listing.buildings.length ? "" : "No buildings found for this area.",
    });
    await refreshPointCloud();
    await refreshTerrain();

    const warnings = (job.warnings || []).slice();
    const pc = state.pointCloud;
    if (pc && pc.total_points > pc.shown_points) {
      warnings.push(
        `Point cloud decimated for display: showing ${pc.shown_points} of ${pc.total_points} points.`
      );
    }
    showWarnings(warnings);
  }

  async function resyncFromServer() {
    try {
      const listing = await apiGetJSON("/api/buildings");
      applyBuildingsResult(listing.buildings);
      await refreshTerrain();
    } catch (err) {
      console.warn("Could not re-sync:", err);
    }
  }

  function finishAreaJob() {
    state.jobId = null;
    setBusy(false);
    hideOverlay();
  }

  function showOverlay(phase, progress, determinate, message) {
    els.loadingOverlay.hidden = false;
    updateOverlayFields(phase, progress, determinate, message);
  }

  function updateOverlay(job) {
    // Polls can complete out of order, so never let the bar go backwards.
    const progress = Math.max(state.jobProgress, job.progress || 0);
    state.jobProgress = progress;
    updateOverlayFields(job.phase, progress, job.determinate, job.message);
  }

  function updateOverlayFields(phase, progress, determinate, message) {
    els.loadingPhase.textContent = phase || "Loading";
    els.loadingMessage.textContent = message || "";
    const track = els.loadingFill.parentElement;
    track.classList.toggle("indeterminate", !determinate);
    els.loadingFill.style.width = determinate ? `${progress}%` : "";
  }

  function hideOverlay() {
    els.loadingOverlay.hidden = true;
  }

  function setBusy(busy) {
    // Nothing disabled these before, so a double-submit fired two full loads.
    for (const el of [els.areaSubmit, els.btnSaveSession, els.btnLoadSession, els.btnExport]) {
      if (el) el.disabled = busy;
    }
  }

  els.btnCancelLoad.addEventListener("click", async () => {
    if (!state.jobId) return;
    els.btnCancelLoad.disabled = true;
    try {
      await apiPostJSON(`/api/area/jobs/${state.jobId}/cancel`);
    } catch (err) {
      showWarnings([`Could not cancel: ${err.message}`]);
    } finally {
      els.btnCancelLoad.disabled = false;
    }
  });

  els.pcColorMode.addEventListener("change", () => {
    if (state.pointCloud) renderPointCloud(state.pointCloud);
  });

  els.pcHideVegetation.addEventListener("change", refreshPointCloud);

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
      renderTerrain(null); // an uploaded session carries no cloud or terrain
      showWarnings([
        "Loaded session. Reload the area to bring back the LiDAR point cloud and terrain.",
      ]);
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
            "Reload the area to bring back the LiDAR point cloud and terrain.",
        ]);
      }
    } catch (err) {
      console.warn("Could not restore autosaved session:", err);
    }
  })();
})();
