import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const script = readFileSync(
  new URL("../../src/chamberlain/surface/viewer/static/appointments.js", import.meta.url),
  "utf8",
);

class Element {
  constructor() {
    this.children = [];
    this.listeners = new Map();
    this.open = false;
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  emit(name, details = {}) {
    const event = { ...details, defaultPrevented: false };
    event.preventDefault = () => { event.defaultPrevented = true; };
    this.listeners.get(name)(event);
    return event;
  }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  focus() {}
  setPointerCapture() {}
  getBoundingClientRect() { return { left: 0, top: 0, right: 100, bottom: 40 }; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

function setup() {
  const requests = [];
  const frames = new Map();
  const document = new Element();
  document.body = new Element();
  const window = new Element();
  let now = 0;
  let nextFrame = 0;
  let renders = 0;
  let button;
  const viewer = {
    el(tag, attrs, ...children) {
      const element = new Element();
      element.replaceChildren(...children);
      if (attrs.class === "ack-button") button = element;
      return element;
    },
    api(method, path, payload) {
      requests.push([method, path, { ...payload }]);
      return Promise.resolve();
    },
    render() { renders += 1; },
  };
  window.ChamberlainViewer = viewer;
  vm.runInNewContext(script, {
    window,
    document,
    performance: { now: () => now },
    requestAnimationFrame(callback) {
      frames.set(++nextFrame, callback);
      return nextFrame;
    },
    cancelAnimationFrame(id) { frames.delete(id); },
  });
  viewer.appointments.check([{ action: "act_fixture", title: "Check-up", due: true }]);
  return {
    button,
    dialog: document.body.children[0],
    document,
    requests,
    renders: () => renders,
    tick(ms) {
      now = ms;
      for (const [id, callback] of [...frames]) {
        frames.delete(id);
        callback(now);
      }
    },
  };
}

const acknowledge = ["POST", "/api/appointments/act_fixture/acknowledge", {}];

test("early release cancels, and a full pointer hold posts once", async () => {
  const { button, dialog, requests, renders, tick } = setup();
  assert.ok(dialog.open);
  assert.ok(dialog.emit("cancel").defaultPrevented);
  const pointer = { pointerId: 1, isPrimary: true, button: 0 };
  button.emit("pointerdown", pointer);
  tick(4999);
  assert.deepEqual(requests, []);
  button.emit("pointerup", pointer);
  tick(5000);
  assert.deepEqual(requests, []);
  button.emit("pointerdown", pointer);
  tick(9999);
  assert.deepEqual(requests, []);
  tick(10000);
  await Promise.resolve();
  assert.equal(dialog.open, false);
  assert.equal(renders(), 1);
  tick(15000);
  assert.deepEqual(requests, [acknowledge]);
});

test("hiding the tab cancels the keyboard hold and a fresh Space hold works", () => {
  const { button, document, requests, tick } = setup();
  button.emit("keydown", { key: "Enter" });
  tick(4999);
  document.hidden = true;
  document.emit("visibilitychange");
  tick(5000);
  assert.deepEqual(requests, []);
  document.hidden = false;
  button.emit("keydown", { key: " " });
  tick(10000);
  assert.deepEqual(requests, [acknowledge]);
});


test("leaving the button or losing keyboard focus resets the elapsed hold", () => {
  const { button, requests, tick } = setup();
  const pointer = { pointerId: 1, isPrimary: true, button: 0 };
  button.emit("pointerdown", pointer);
  tick(4999);
  button.emit("pointermove", { pointerId: 1, clientX: 101, clientY: 20 });
  tick(5000);
  assert.deepEqual(requests, []);

  button.emit("keydown", { key: "Enter" });
  tick(9999);
  button.emit("blur");
  tick(10000);
  assert.deepEqual(requests, []);
});
