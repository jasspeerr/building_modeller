/* Building Modeller frontend: talks to the Flask REST API and renders
 * footprints / LiDAR point cloud / the live building mesh with Three.js.
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
    roofForm: document.getElementById("roof-form"),
    buildingPanelEmpty: document.getElementById("building-panel-empty"),
    roofBagId: document.getElementById("roof-bag-id"),
    roofLidarStats: document.getElementById("roof-lidar-stats"),
    roofType: document.getElementById("roof-type"),
    roofRidgeAlong: document.getElementById("roof-ridge-along"),
    roofEave: document.getElementById("roof-eave"),
    roofRidge: document.getElementById("roof-ridge"),
    btnLidarSuggest: document.getElementById("btn-lidar-suggest"),
    btnSaveSession: document.getElementById("btn-save-session"),
    btnLoadSession: document.getElementById("btn-load-session"),
    loadSessionFile: document.getElementById("load-session-file"),
    btnExport: document.getElementById("btn-export"),
  };

  const state = {
    buildings: [],
    selectedBagId: null,
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
      body: JSON.stringify(body),
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
    geometry.setIndex(meshData.faces);
    geometry.computeVertexNormals();
    const material = new THREE.MeshStandardMaterial({
      color: 0xd9522e,
      side: THREE.DoubleSide,
      flatShading: true,
    });
    buildingMeshObject = new THREE.Mesh(geometry, material);
    scene.add(buildingMeshObject);
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
    renderBuildingList();
    const building = state.buildings.find((b) => b.bag_id === bagId);
    if (!building) return;
    fillRoofForm(building);
    const meshData = await apiGetJSON(`/api/buildings/${encodeURIComponent(bagId)}/mesh`);
    renderBuildingMesh(meshData);
  }

  function fillRoofForm(building) {
    state.updatingPanel = true;
    try {
      els.buildingPanelEmpty.hidden = true;
      els.roofForm.hidden = false;
      els.roofBagId.textContent = building.bag_id;

      const stats = building.lidar_stats;
      els.roofLidarStats.textContent = stats
        ? `${stats.point_count} pts, ground ${stats.ground_height.toFixed(2)} m, top ${stats.top_height.toFixed(2)} m`
        : "no LiDAR stats";

      const roof = building.roof;
      if (roof) {
        els.roofType.value = roof.roof_type;
        els.roofRidgeAlong.value = roof.ridge_along;
        els.roofEave.value = roof.eave_height;
        els.roofRidge.value = roof.ridge_height != null ? roof.ridge_height : roof.eave_height;
      } else {
        const eave = stats ? stats.eave_estimate : building.ground_height + 3.0;
        const ridge = stats ? stats.ridge_estimate : eave;
        els.roofType.value = "flat";
        els.roofRidgeAlong.value = "long";
        els.roofEave.value = eave.toFixed(2);
        els.roofRidge.value = ridge.toFixed(2);
      }
      updateRoofFieldAvailability();
    } finally {
      state.updatingPanel = false;
    }
  }

  function updateRoofFieldAvailability() {
    const isFlat = els.roofType.value === "flat";
    els.roofRidge.disabled = isFlat;
    const isHipOrGable = els.roofType.value === "gable" || els.roofType.value === "hip";
    els.roofRidgeAlong.disabled = !isHipOrGable;
  }

  async function submitRoofChange() {
    if (state.updatingPanel || !state.selectedBagId) return;
    const roofType = els.roofType.value;
    const body = {
      roof_type: roofType,
      eave_height: parseFloat(els.roofEave.value),
      ridge_height: roofType === "flat" ? null : parseFloat(els.roofRidge.value),
      ridge_along: els.roofRidgeAlong.value,
    };
    const result = await apiPostJSON(
      `/api/buildings/${encodeURIComponent(state.selectedBagId)}/roof`,
      body
    );
    const idx = state.buildings.findIndex((b) => b.bag_id === state.selectedBagId);
    if (idx >= 0) state.buildings[idx] = result.building;
    renderBuildingList();
    renderBuildingMesh(result.mesh);
  }

  els.roofType.addEventListener("change", () => {
    updateRoofFieldAvailability();
    submitRoofChange();
  });
  els.roofRidgeAlong.addEventListener("change", submitRoofChange);
  els.roofEave.addEventListener("change", submitRoofChange);
  els.roofRidge.addEventListener("change", submitRoofChange);

  els.btnLidarSuggest.addEventListener("click", () => {
    const building = state.buildings.find((b) => b.bag_id === state.selectedBagId);
    if (!building || !building.lidar_stats) return;
    els.roofEave.value = building.lidar_stats.eave_estimate.toFixed(2);
    els.roofRidge.value = building.lidar_stats.ridge_estimate.toFixed(2);
    submitRoofChange();
  });

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
      state.buildings = result.buildings;
      state.selectedBagId = null;
      showWarnings(result.warnings);
      renderFootprints(state.buildings);
      renderPointCloud(result.point_cloud);
      renderBuildingMesh(null);
      renderBuildingList();
      els.buildingPanelEmpty.hidden = false;
      els.roofForm.hidden = true;
      els.viewportHint.textContent = state.buildings.length
        ? ""
        : "No buildings found for this area.";
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
      state.buildings = result.buildings;
      state.selectedBagId = null;
      renderFootprints(state.buildings);
      renderPointCloud({ x: [], y: [], z: [] });
      renderBuildingMesh(null);
      renderBuildingList();
      els.buildingPanelEmpty.hidden = false;
      els.roofForm.hidden = true;
      showWarnings([]);
      els.viewportHint.textContent = "";
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
})();
