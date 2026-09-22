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
  bg: '#050608',
  main: '#FFB000',
  bright: '#FFD25A',
  glow: '#FF7A00',
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
  eyeY: 74,
  eyeLeftX: 104,
  eyeRightX: 216,
  eyeW: 26,
  eyeH: 30,
  eyeCut: 6,
  pupilW: 10,
  pupilH: 12,
  mouthY: 132,
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

  drawEyes(now) {
    const closed = this.blink > 0.75 || [FaceState.SLEEPING].includes(this.state);
    const surprised = this.state === FaceState.SURPRISED;
    const happyArc = this.state === FaceState.HAPPY;
    const errorX = this.state === FaceState.ERROR;

    for (const side of [-1, 1]) {
      const cx = 160 + side * (G.eyeRightX - 160);
      const eyeH = surprised ? G.eyeH + 8 : G.eyeH;
      const eyeW = surprised ? G.eyeW + 4 : G.eyeW;
      const x = cx - eyeW / 2;
      const y = G.eyeY - eyeH / 2;
      const gx = this.gaze.x;
      const gy = this.gaze.y;

      if (errorX) {
        // крестики вместо глаз
        const n = 6, s = 6, step = 5;
        for (let i = 0; i < n; i++) {
          this.r(x + i * step, y + i * step, s, s, FACE_COLORS.main);
          this.r(x + (n - 1 - i) * step, y + i * step, s, s, FACE_COLORS.main);
        }
        continue;
      }
      if (happyArc) {
        // счастливые «дуги» (перевёрнутая скобка из блоков)
        const w = eyeW + 6;
        for (let i = 0; i < 6; i++) {
          const t = i / 5;
          const bx = x + t * (w - 6);
          const by = y + 10 - Math.sin(t * Math.PI) * 9;
          this.r(bx, by, 6, 6, FACE_COLORS.bright);
        }
        continue;
      }
      if (closed) {
        // закрытые глаза: линия (спим) или мигание
        const lineH = this.state === FaceState.SLEEPING ? 4 : Math.max(3, eyeH * (1 - this.blink));
        const lineY = this.state === FaceState.SLEEPING ? G.eyeY - 2 : y + (eyeH - lineH) / 2;
        this.roundBlocky(x, lineY, eyeW, lineH, 2, FACE_COLORS.main);
        continue;
      }

      // обычный глаз: свечение -> основной контур -> зрачок -> блик
      this.roundBlocky(x - 3, y - 3, eyeW + 6, eyeH + 6, G.eyeCut + 2, FACE_COLORS.glow);
      this.roundBlocky(x, y, eyeW, eyeH, G.eyeCut, FACE_COLORS.main);
      const px = cx - G.pupilW / 2 + gx;
      const py = G.eyeY - G.pupilH / 2 + gy;
      this.roundBlocky(px, py, G.pupilW, G.pupilH, 2, FACE_COLORS.bg);
      this.r(px + 1, py + 1, 4, 4, FACE_COLORS.bright);
    }
  }

  drawBrows() {
    if (this.state === FaceState.SLEEPING) return;   // спящему брови не нужны
    const lift = this.state === FaceState.SURPRISED ? 10 : 0;
    const sad = this.state === FaceState.ERROR ? 6 : 0;
    for (const side of [-1, 1]) {
      const cx = 160 + side * (G.eyeRightX - 160);
      const x = cx - (G.eyeW + 8) / 2;
      const y = G.eyeY - G.eyeH / 2 - 14 - lift + sad;
      const tilt = side * (sad ? 4 : 0);
      for (let i = 0; i < 6; i++) {
        this.r(x + i * 6, y + tilt * (i / 5), 5, 4, FACE_COLORS.main);
      }
    }
  }

  drawMouth() {
    const cx = 160;
    const y = G.mouthY;
    switch (this.state) {
      case FaceState.SPEAKING: {
        const level = this.audioLevel > 0.01 ? this.audioLevel : this.audioAuto;
        const h = 6 + level * 22;
        const w = 30 + level * 26;
        this.roundBlocky(cx - w / 2, y - h / 2, w, h, 4, FACE_COLORS.main);
        this.r(cx - w / 2 + 6, y + h / 2 - 6, w - 12, 3, FACE_COLORS.bright);
        break;
      }
      case FaceState.HAPPY: {
        for (let i = 0; i < 7; i++) {
          const t = i / 6;
          const bx = cx - 34 + t * 62;
          const by = y - 6 + Math.sin(t * Math.PI) * 12;
          this.r(bx, by, 6, 6, FACE_COLORS.bright);
        }
        break;
      }
      case FaceState.SURPRISED:
        this.roundBlocky(cx - 9, y - 9, 18, 18, 4, FACE_COLORS.main);
        this.r(cx - 4, y - 4, 8, 8, FACE_COLORS.bg);
        break;
      case FaceState.SLEEPING:
        this.r(cx - 10, y, 20, 4, FACE_COLORS.main);
        break;
      case FaceState.THINKING:
        this.r(cx - 16, y, 10, 4, FACE_COLORS.main);
        break;
      case FaceState.ERROR:
        this.r(cx - 18, y + 4, 36, 4, FACE_COLORS.main);
        break;
      case FaceState.LISTENING:
        this.roundBlocky(cx - 12, y - 4, 24, 8, 3, FACE_COLORS.main);
        break;
      default: { // IDLE — небольшая улыбка
        for (let i = 0; i < 5; i++) {
          const t = i / 4;
          const bx = cx - 20 + t * 36;
          const by = y - 4 + Math.sin(t * Math.PI) * 7;
          this.r(bx, by, 5, 5, FACE_COLORS.main);
        }
      }
    }
  }

  drawListeningBars() {
    // столбики «слышу» под глазами, как эквалайзер
    const baseY = 108;
    for (let s = 0; s < 2; s++) {
      const cx = 160 + (s === 0 ? -1 : 1) * (G.eyeRightX - 160);
      for (let i = 0; i < 5; i++) {
        const level = 0.25 + 0.75 * Math.abs(Math.sin(this.t * 6 + i * 0.9 + s * 1.7));
        const h = 4 + level * 14;
        this.r(cx - 16 + i * 7, baseY - h / 2, 5, h, i % 2 ? FACE_COLORS.bright : FACE_COLORS.main);
      }
    }
  }

  drawThinkingDots() {
    for (let i = 0; i < 3; i++) {
      const on = Math.floor(this.t * 2.5) % 3 >= i;
      this.r(258 + i * 12, 108, 8, 8, on ? FACE_COLORS.bright : FACE_COLORS.glow);
    }
  }

  drawSleepingZ() {
    const zs = [[236, 60, 4], [258, 38, 5], [282, 16, 6]];
    let index = 0;
    for (const [zx, zy, s] of zs) {
      // у каждой «z» своя фаза — буквы не слипаются
      const x = zx + Math.sin(this.t * 1.4 + index * 1.1) * 4;
      index++;
      // буква Z: верхняя перекладина, диагональ, нижняя перекладина
      this.r(x, zy, s * 5, s, FACE_COLORS.main);
      for (let i = 0; i < 5; i++) {
        this.r(x + (4 - i) * s, zy + s + i * s, s, s, FACE_COLORS.main);
      }
      this.r(x, zy + s * 6, s * 5, s, FACE_COLORS.main);
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
