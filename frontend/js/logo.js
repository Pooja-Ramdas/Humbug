/**
 * logo.js — Three.js animated hexagonal bolt logo with 2D Canvas fallback
 *
 * Renders a hexagonal prism bolt with neon cyberpunk stripes, rotating
 * continuously about its Y axis.
 */

(function () {
  'use strict';

  function startThreeLogo(canvas) {
    const W = canvas.width || 44;
    const H = canvas.height || 44;

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    renderer.setSize(W, H);
    renderer.setClearColor(0x000000, 0);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(38, W / H, 0.1, 100);
    camera.position.set(0, 0, 3.8);

    const ambient = new THREE.AmbientLight(0xffffff, 0.4);
    scene.add(ambient);

    const key = new THREE.DirectionalLight(0x00f0ff, 1.8);
    key.position.set(2, 3, 4);
    scene.add(key);

    const fill = new THREE.DirectionalLight(0xff0055, 0.8);
    fill.position.set(-3, -1, 2);
    scene.add(fill);

    const rim = new THREE.DirectionalLight(0xffe600, 0.5);
    rim.position.set(0, -4, -2);
    scene.add(rim);

    const geo = new THREE.CylinderGeometry(1, 1, 0.52, 6, 1);

    const mat = new THREE.MeshStandardMaterial({
      color: 0x0c1424,
      metalness: 0.7,
      roughness: 0.25,
      emissive: 0x002233,
      emissiveIntensity: 0.3,
    });

    const capMat = new THREE.MeshStandardMaterial({
      color: 0x00f0ff,
      metalness: 0.4,
      roughness: 0.3,
      emissive: 0x00f0ff,
      emissiveIntensity: 0.6,
    });

    const bolt = new THREE.Mesh(geo, [mat, capMat, mat]);
    scene.add(bolt);

    const edgeGeo = new THREE.CylinderGeometry(1.02, 1.02, 0.14, 6, 1, true);
    const edgeMat = new THREE.MeshBasicMaterial({
      color: 0x00f0ff,
      transparent: true,
      opacity: 0.85,
      side: THREE.FrontSide,
    });
    const edgeRing = new THREE.Mesh(edgeGeo, edgeMat);
    scene.add(edgeRing);

    const band2Geo = new THREE.CylinderGeometry(1.025, 1.025, 0.08, 6, 1, true);
    const band2Mat = new THREE.MeshBasicMaterial({
      color: 0xff0055,
      transparent: true,
      opacity: 0.7,
      side: THREE.FrontSide,
    });
    const band2 = new THREE.Mesh(band2Geo, band2Mat);
    band2.position.y = 0.14;
    scene.add(band2);

    const band3 = new THREE.Mesh(band2Geo.clone(), new THREE.MeshBasicMaterial({
      color: 0xffe600,
      transparent: true,
      opacity: 0.55,
      side: THREE.FrontSide,
    }));
    band3.position.y = -0.14;
    scene.add(band3);

    const wireGeo = new THREE.EdgesGeometry(geo);
    const wireMat = new THREE.LineBasicMaterial({
      color: 0x00f0ff,
      transparent: true,
      opacity: 0.35,
    });
    const wireframe = new THREE.LineSegments(wireGeo, wireMat);
    scene.add(wireframe);

    let frame;
    let lastTime = performance.now();
    const RPM = 24;

    function animate(now) {
      frame = requestAnimationFrame(animate);
      const dt = Math.min((now - lastTime) / 1000, 0.1);
      lastTime = now;

      const angle = dt * (RPM / 60) * Math.PI * 2;
      bolt.rotation.y      += angle;
      edgeRing.rotation.y  += angle;
      band2.rotation.y     += angle;
      band3.rotation.y     += angle;
      wireframe.rotation.y += angle;

      const t = now / 1000;
      const wobble = Math.sin(t * 0.7) * 0.12;
      bolt.rotation.x      = wobble;
      edgeRing.rotation.x  = wobble;
      band2.rotation.x     = wobble;
      band3.rotation.x     = wobble;
      wireframe.rotation.x = wobble;

      renderer.render(scene, camera);
    }

    requestAnimationFrame(animate);

    window.addEventListener('beforeunload', () => {
      cancelAnimationFrame(frame);
      renderer.dispose();
    });
  }

  function start2DFallbackLogo(canvas) {
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let angle = 0;
    let frame;

    function draw() {
      frame = requestAnimationFrame(draw);
      angle += 0.03;
      const w = canvas.width;
      const h = canvas.height;
      const cx = w / 2;
      const cy = h / 2;
      const r = Math.min(w, h) * 0.38;

      ctx.clearRect(0, 0, w, h);
      ctx.save();
      ctx.translate(cx, cy);

      // Outer glowing hexagon ring
      ctx.beginPath();
      for (let i = 0; i < 6; i++) {
        const a = angle + (i * Math.PI) / 3;
        const x = r * Math.cos(a);
        const y = r * Math.sin(a);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.fillStyle = '#0c1424';
      ctx.fill();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = '#00f0ff';
      ctx.shadowColor = '#00f0ff';
      ctx.shadowBlur = 8;
      ctx.stroke();

      // Inner accents
      ctx.beginPath();
      for (let i = 0; i < 6; i++) {
        const a = angle + (i * Math.PI) / 3;
        const x = r * 0.6 * Math.cos(a);
        const y = r * 0.6 * Math.sin(a);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.strokeStyle = '#ff0055';
      ctx.shadowColor = '#ff0055';
      ctx.shadowBlur = 5;
      ctx.lineWidth = 1.5;
      ctx.stroke();

      ctx.restore();
    }

    requestAnimationFrame(draw);
    window.addEventListener('beforeunload', () => cancelAnimationFrame(frame));
  }

  function init() {
    const canvas = document.getElementById('logo-3d');
    if (!canvas) return;

    let attempts = 0;
    function checkAndStart() {
      attempts++;
      if (typeof THREE !== 'undefined') {
        try {
          startThreeLogo(canvas);
          return;
        } catch (e) {
          console.warn('[HumbugLogo] Three.js init error, fallback to 2D:', e);
        }
      }
      if (attempts < 10) {
        setTimeout(checkAndStart, 200);
      } else {
        start2DFallbackLogo(canvas);
      }
    }

    checkAndStart();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
