/*
 * Движок лица Stack-chan для экрана 320x172 (ESP32-S3-LCD-1.47B-M, ST7789, альбом).
 *
 * Модель отрисовки повторяет будущую прошивку: только примитивы и блоки, без картинок.
 * При портировании на ESP32:
 *   - r(x,y,w,h,c)            -> display.fillRect(...)
 *   - roundBlocky(...)        -> несколько fillRect (углы срезаются фоном)
 *   - tick(dt)/render()       -> задача анимации ~30 FPS, кадр рисуется в Sprite и одним
 *                                вызовом выводится на экран (двойной буфер в PSRAM)
 *   - FaceState + setState()  -> enum FaceState и таблица переходов
 *   - audioLevel              -> амплитуда из I2S (MAX98357) во время SPEAKING
 *   - gaze                    -> события move() и (позже) гироскоп QMI8658
 *   - showPhoto()             -> JPEG на дисплее на 6 секунд и обратно к лицу
 */
const FACE_WIDTH = 320;
const FACE_HEIGHT = 172;

const FACE_COLORS = {
  // Палитра ближе к референсу: почти чёрный фон, янтарный основной,
  // тёплый яркий блик и приглушённый оранжевый псевдо-glow.
  bg: '#050608',
  main: '#FFB21A',
  bright: '#FFD35A',
  glow: '#7A3200',
};

const FaceState = {
  IDLE: 'IDLE',
  LISTENING: 'LISTENING',
  THINKING: 'THINKING',
  SPEAKING: 'SPEAKING',
  HAPPY: 'HAPPY',
  SURPRISED: 'SURPRISED',
  SLEEPING: 'SLEEPING',
  ERROR: 'ERROR',
};

// Геометрия (единые константы — те же значения пойдут в прошивку).
const G = {
  // В референсе лицо крупнее: глаза занимают почти всю верхнюю половину дисплея.
  eyeY: 59,
  eyeLeftX: 101,
  eyeRightX: 219,
  eyeW: 54,
  eyeH: 48,
  eyeThickness: 6,
  mouthY: 111,
  gazeMaxX: 6,
  gazeMaxY: 4,
};

class StackchanFace {
  constructor(canvas) {
    this.canvas = canvas;
    canvas.width = FACE_WIDTH;
    canvas.height = FACE_HEIGHT;
    this.ctx = canvas.getContext('2d');

    this.state = FaceState.IDLE;
    this.stateUntil = 0;          // временное состояние (HAPPY/SURPRISED) до этого ts
    this.stateAfter = FaceState.IDLE;

    this.blink = 0;               // 0 — открыт, 1 — закрыт
    this.blinkPhase = -1;         // -1 — не моргает
    this.nextBlinkAt = performance.now() + 2000 + Math.random() * 4000;

    this.gaze = { x: 0, y: 0 };
    this.gazeTarget = { x: 0, y: 0 };
    this.nextGlanceAt = performance.now() + 3000 + Math.random() * 4000;

    this.audioLevel = 0;          // 0..1, только для SPEAKING
    this.audioAuto = 0;           // резервный «рот по таймеру», если уровень недоступен
    this.t = 0;
    this.photo = null;            // { img, until } — показать кадр поверх лица
  }

  setState(name, holdMs = 0, after = FaceState.IDLE) {
    if (!(name in FaceState)) return;
    this.state = name;
    if (holdMs > 0) {
      this.stateUntil = performance.now() + holdMs;
      this.stateAfter = after;
    } else {
      this.stateUntil = 0;
    }
  }

  setGaze(dx, dy) {
    this.gazeTarget.x = Math.max(-G.gazeMaxX, Math.min(G.gazeMaxX, dx));
    this.gazeTarget.y = Math.max(-G.gazeMaxY, Math.min(G.gazeMaxY, dy));
  }

  setAudioLevel(level) {
    this.audioLevel = Math.max(0, Math.min(1, level));
  }

  showPhoto(img, holdMs = 6000) {
    this.photo = { img, until: performance.now() + holdMs };
  }

  // --- анимация ---

  tick(dt, now) {
    this.t += dt;

    if (this.stateUntil && now >= this.stateUntil) {
      this.setState(this.stateAfter);
    }

    // моргание
    if (this.blinkPhase < 0 && now >= this.nextBlinkAt) {
      this.blinkPhase = 0;
    }
    if (this.blinkPhase >= 0) {
      this.blinkPhase += dt / 0.28;               // вся вспышка ~0.3 c
      this.blink = this.blinkPhase <= 0.5
        ? this.blinkPhase * 2
        : Math.max(0, 2 - this.blinkPhase * 2);
      if (this.blinkPhase >= 1) {
        this.blinkPhase = -1;
        this.blink = 0;
        this.nextBlinkAt = now + 2000 + Math.random() * 5000;
      }
    }

    // случайные взгляды в IDLE
    if (this.state === FaceState.IDLE && now >= this.nextGlanceAt) {
      this.setGaze((Math.random() * 2 - 1) * G.gazeMaxX, (Math.random() * 2 - 1) * G.gazeMaxY * 0.5);
      this.nextGlanceAt = now + 2500 + Math.random() * 5000;
      setTimeout(() => {
        if (this.state === FaceState.IDLE) this.setGaze(0, 0);
      }, 800 + Math.random() * 900);
    }

    // плавное движение взгляда
    this.gaze.x += (this.gazeTarget.x - this.gaze.x) * Math.min(1, dt * 10);
    this.gaze.y += (this.gazeTarget.y - this.gaze.y) * Math.min(1, dt * 10);

    // резервная анимация рта, если реальный уровень звука не приходит
    if (this.state === FaceState.SPEAKING && this.audioLevel <= 0.01) {
      this.audioAuto = 0.5 + 0.5 * Math.sin(this.t * 9) * Math.sin(this.t * 3.7);
    } else {
      this.audioAuto = 0;
    }

    if (this.photo && now >= this.photo.until) {
      this.photo = null;
    }
  }

  // --- примитивы (при портировании -> display.fillRect) ---

  r(x, y, w, h, color) {
    this.ctx.fillStyle = color;
    this.ctx.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h));
  }

  roundBlocky(x, y, w, h, cut, color) {
    this.r(x, y, w, h, color);
    const bg = FACE_COLORS.bg;
    this.r(x, y, cut, cut, bg);
    this.r(x + w - cut, y, cut, cut, bg);
    this.r(x, y + h - cut, cut, cut, bg);
    this.r(x + w - cut, y + h - cut, cut, cut, bg);
  }

  // --- части лица ---

  // Пиксельное восьмиугольное кольцо. В отличие от прежнего roundBlocky()
  // глаз НЕ является залитым прямоугольником: это именно толстый пустой контур,
  // как на референсе с янтарными круглыми/восьмиугольными глазами.
  drawOctoRing(cx, cy, w, h, t, color, withGlow = true) {
    if (withGlow) {
      this.drawOctoRing(cx, cy, w + 4, h + 4, t + 2, FACE_COLORS.glow, false);
    }

    const x = Math.round(cx - w / 2);
    const y = Math.round(cy - h / 2);
    const cut = t * 2;

    this.r(x + cut, y, w - cut * 2, t, color);                   // top
    this.r(x + cut, y + h - t, w - cut * 2, t, color);           // bottom
    this.r(x, y + cut, t, h - cut * 2, color);                   // left
    this.r(x + w - t, y + cut, t, h - cut * 2, color);           // right

    this.r(x + t, y + t, t, t, color);                           // TL
    this.r(x + w - t * 2, y + t, t, t, color);                   // TR
    this.r(x + t, y + h - t * 2, t, t, color);                   // BL
    this.r(x + w - t * 2, y + h - t * 2, t, t, color);           // BR
  }

  // Блочная дуга: mode='u' — улыбка/нижняя дуга, mode='cap' — верхняя дуга.
  drawPixelCurve(cx, cy, w, h, mode, color, block = 6, withGlow = true) {
    const n = 7;
    const points = [];
    for (let i = 0; i < n; i++) {
      const p = i / (n - 1);
      const x = cx - w / 2 + p * w;
      const s = Math.sin(p * Math.PI);
      const y = mode === 'u'
        ? cy - h / 2 + s * h
        : cy + h / 2 - s * h;
      points.push([x, y]);
    }

    if (withGlow) {
      for (const [x, y] of points) {
        this.r(x - block / 2 - 1, y - block / 2 - 1, block + 2, block + 2, FACE_COLORS.glow);
      }
    }
    for (const [x, y] of points) {
      this.r(x - block / 2, y - block / 2, block, block, color);
    }
  }

  drawPixelX(cx, cy, size, color) {
    const block = 6;
    const steps = 5;
    for (let i = 0; i < steps; i++) {
      const p = i / (steps - 1);
      const dx = -size / 2 + p * size;
      const dy = -size / 2 + p * size;
      this.r(cx + dx - block / 2, cy + dy - block / 2, block, block, color);
      this.r(cx - dx - block / 2, cy + dy - block / 2, block, block, color);
    }
  }

  drawCheeks() {
    const y = 92;
    for (const side of [-1, 1]) {
      const x = 160 + side * 77;
      this.r(x - 7, y, 5, 5, FACE_COLORS.glow);
      this.r(x + 2, y + 3, 5, 5, FACE_COLORS.main);
    }
  }

  drawEyes() {
    const closedByBlink = this.blink > 0.62;
    const state = this.state;

    for (const side of [-1, 1]) {
      const baseCx = 160 + side * (G.eyeRightX - 160);
      const cx = baseCx + this.gaze.x * 0.45;
      const cy = G.eyeY + this.gaze.y * 0.35;

      if (state === FaceState.ERROR) {
        this.drawPixelX(cx, cy, 34, FACE_COLORS.main);
        continue;
      }

      if (state === FaceState.SLEEPING) {
        // В референсе сон — мягкие U-образные закрытые глаза.
        this.drawPixelCurve(cx, cy, 40, 13, 'u', FACE_COLORS.main, 6, true);
        continue;
      }

      if (state === FaceState.HAPPY) {
        // Закрытые "улыбающиеся" глаза.
        this.drawPixelCurve(cx, cy, 43, 14, 'cap', FACE_COLORS.bright, 6, true);
        continue;
      }

      if (state === FaceState.SPEAKING && side === -1) {
        // Небольшой подмигивающий левый глаз как на референсе SPEAKING.
        this.drawPixelCurve(cx, cy, 40, 13, 'cap', FACE_COLORS.bright, 6, true);
        continue;
      }

      if (closedByBlink) {
        this.r(cx - 19, cy - 3, 38, 6, FACE_COLORS.glow);
        this.r(cx - 17, cy - 2, 34, 5, FACE_COLORS.main);
        continue;
      }

      const surprised = state === FaceState.SURPRISED;
      const thinking = state === FaceState.THINKING;
      const w = surprised ? 60 : (thinking ? 50 : G.eyeW);
      const h = surprised ? 54 : (thinking ? 45 : G.eyeH);

      this.drawOctoRing(cx, cy, w, h, G.eyeThickness, surprised ? FACE_COLORS.bright : FACE_COLORS.main, true);
    }
  }

  drawBrows() {
    if (this.state === FaceState.SLEEPING || this.state === FaceState.ERROR) return;

    const surprised = this.state === FaceState.SURPRISED;
    const happy = this.state === FaceState.HAPPY;
    const y = surprised ? 18 : 23;

    for (const side of [-1, 1]) {
      const cx = 160 + side * (G.eyeRightX - 160);
      this.drawPixelCurve(
        cx,
        y + (happy ? 2 : 0),
        surprised ? 25 : 22,
        surprised ? 9 : 7,
        'cap',
        FACE_COLORS.main,
        4,
        false
      );
    }

    if (surprised) {
      // Два "!" справа — характерная деталь WOW из референса.
      this.r(274, 24, 5, 18, FACE_COLORS.bright);
      this.r(274, 47, 5, 5, FACE_COLORS.main);
      this.r(287, 29, 5, 14, FACE_COLORS.main);
      this.r(287, 48, 5, 5, FACE_COLORS.glow);
    }
  }

  drawSpeechBars() {
    const baseY = 149;
    const count = 9;
    for (let i = 0; i < count; i++) {
      const phase = this.t * 8 + i * 0.65;
      const h = 7 + Math.abs(Math.sin(phase)) * 17;
      const x = 160 - ((count - 1) * 7) / 2 + i * 7;
      this.r(x - 2, baseY - h / 2, 4, h, i % 2 ? FACE_COLORS.bright : FACE_COLORS.main);
    }
  }

  drawMouth() {
    const cx = 160;
    const y = G.mouthY;

    switch (this.state) {
      case FaceState.SPEAKING: {
        const level = this.audioLevel > 0.01 ? this.audioLevel : this.audioAuto;
        const w = 24 + level * 22;
        const h = 20 + level * 18;

        this.roundBlocky(cx - w / 2 - 2, y - h / 2 - 2, w + 4, h + 4, 6, FACE_COLORS.glow);
        this.roundBlocky(cx - w / 2, y - h / 2, w, h, 5, FACE_COLORS.main);
        this.r(cx - 6, y - h / 2 + 5, 12, 4, FACE_COLORS.bright);
        this.drawSpeechBars();
        break;
      }

      case FaceState.HAPPY:
        this.drawPixelCurve(cx, y, 42, 13, 'u', FACE_COLORS.bright, 6, true);
        this.drawCheeks();
        break;

      case FaceState.SURPRISED:
        this.roundBlocky(cx - 7, y - 7, 14, 14, 4, FACE_COLORS.glow);
        this.roundBlocky(cx - 5, y - 5, 10, 10, 3, FACE_COLORS.bright);
        break;

      case FaceState.SLEEPING:
        // Маленькая точка-"носик" между U-глазами, как в референсе SLEEP MODE.
        this.roundBlocky(cx - 4, y - 4, 8, 8, 2, FACE_COLORS.main);
        break;

      case FaceState.THINKING:
        this.r(cx - 13, y - 2, 26, 5, FACE_COLORS.main);
        break;

      case FaceState.ERROR:
        this.drawPixelCurve(cx, y + 2, 38, 12, 'cap', FACE_COLORS.main, 6, true);
        break;

      case FaceState.LISTENING:
        this.roundBlocky(cx - 4, y - 4, 8, 8, 2, FACE_COLORS.main);
        break;

      default:
        this.drawPixelCurve(cx, y, 34, 10, 'u', FACE_COLORS.bright, 6, true);
        break;
    }
  }

  drawListeningBars() {
    // Один центральный эквалайзер в нижней части экрана — ближе к референсу.
    const baseY = 148;
    const count = 9;
    for (let i = 0; i < count; i++) {
      const p = i / (count - 1);
      const wave = 0.35 + 0.65 * Math.abs(Math.sin(this.t * 6.5 + i * 0.8));
      const envelope = 0.65 + 0.35 * Math.sin(p * Math.PI);
      const h = 5 + wave * envelope * 22;
      const x = 160 - 32 + i * 8;
      this.r(x - 2, baseY - h / 2, 5, h, i % 2 ? FACE_COLORS.bright : FACE_COLORS.main);
    }
  }

  drawThinkingDots() {
    // Три точки в правом верхнем углу — как у THINKING на референсе.
    for (let i = 0; i < 3; i++) {
      const active = (Math.floor(this.t * 3) + i) % 3;
      const s = i === 1 ? 7 : 6;
      const x = 255 + i * 13;
      const y = 28 + i * 3;
      this.r(x, y, s, s, active === 0 ? FACE_COLORS.bright : FACE_COLORS.glow);
    }
  }

  drawSleepingZ() {
    const zs = [[238, 45, 3], [257, 28, 4], [280, 11, 5]];
    let index = 0;
    for (const [zx, zy, s] of zs) {
      const x = zx + Math.sin(this.t * 1.25 + index * 1.2) * 3;
      index++;

      this.r(x, zy, s * 4, s, FACE_COLORS.main);
      for (let i = 0; i < 4; i++) {
        this.r(x + (3 - i) * s, zy + s + i * s, s, s, FACE_COLORS.main);
      }
      this.r(x, zy + s * 5, s * 4, s, FACE_COLORS.main);
    }
  }

  // --- кадр ---

  render(now) {
    const ctx = this.ctx;
    ctx.fillStyle = FACE_COLORS.bg;
    ctx.fillRect(0, 0, FACE_WIDTH, FACE_HEIGHT);

    if (this.photo) {
      // камера: показываем кадр целиком, как прошивка показывает JPEG 6 секунд
      const img = this.photo.img;
      const scale = Math.min(FACE_WIDTH / img.width, FACE_HEIGHT / img.height);
      const w = img.width * scale;
      const h = img.height * scale;
      ctx.drawImage(img, (FACE_WIDTH - w) / 2, (FACE_HEIGHT - h) / 2, w, h);
      return;
    }

    this.drawEyes(now);
    this.drawBrows();
    this.drawMouth();

    if (this.state === FaceState.LISTENING) this.drawListeningBars();
    if (this.state === FaceState.THINKING || this.state === FaceState.SPEAKING) {
      if (this.state === FaceState.THINKING) this.drawThinkingDots();
    }
    if (this.state === FaceState.SLEEPING) this.drawSleepingZ();
  }

  start() {
    let last = performance.now();
    const loop = (now) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      this.tick(dt, now);
      this.render(now);
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }
}

if (typeof window !== 'undefined') {
  window.StackchanFace = StackchanFace;
  window.FaceState = FaceState;
  window.FACE_COLORS = FACE_COLORS;
}
