# TexSet Spotlight

A small plugin for **Adobe Substance 3D Painter**: select a texture set in the Texture Set List and the
meshes that use it flash in the 3D viewport, then fade out. With X-ray on, everything else turns
semi-transparent during the flash so you can also spot parts hidden inside the model.

![X-ray flash: the selected texture set glows green through the ghosted body](docs/spotlight_xray.png)

![Plain flash](docs/spotlight_flash.png)

(Model in the screenshots: "O-TECH Reaper-Death Stranding Chiral" by chiwei on Sketchfab, CC BY 4.0.)

## Features

- Flash on selection: hold for 1 s, fade out over 1 s (both adjustable)
- X-ray: other texture sets are drawn at 12 % opacity and dimmed while the flash runs (adjustable, can be turned off)
- Works with any number of texture sets; switching 40 sets takes about 0.2 s
- Never touches your layer stacks. Shader assignments are restored when the flash ends, before every save/close,
  and on the next open if Painter crashed in the middle of a flash
- "Spotlight" menu in the menu bar: enable/disable, X-ray, flash now, color, hold/fade time, intensity, X-ray opacity/brightness.
  Settings persist between sessions

## Install

1. Copy `texset_spotlight.py` and `texset_spotlight_shader.glsl` into Painter's plugins folder:
   - Windows: `Documents\Adobe\Adobe Substance 3D Painter\python\plugins\`
   - macOS: `~/Documents/Adobe/Adobe Substance 3D Painter/python/plugins/`
2. Start Painter (or Python > Reload Plugins Folder, then enable `texset_spotlight` in the Python menu).
3. A **Spotlight** menu appears in the menu bar.

Requirements: Substance 3D Painter 12.x. No extra Python packages (uses the PySide6 bundled with Painter).

### Tested with

| | |
|---|---|
| Adobe Substance 3D Painter | **12.1.1** (Vulkan renderer) |
| Painter Python API | 0.3.5 (Python 3.13.9, PySide6 6.8.6) |
| Painter JavaScript API (`alg`) | 1.1.20 |
| OS / GPU | Windows 11 (26200) / NVIDIA GeForce RTX 5060 Ti |
| Project | 40 texture sets at 1024², single shader instance |

Other 12.x releases should work (the plugin only uses `alg.shaders`, `substance_painter.textureset`,
`substance_painter.ui` and `substance_painter.event`), but they have not been checked. Painter 11 and older use
PySide2 and are not supported as-is.

## How it works

Painter has no API for drawing overlays in the viewport, so the plugin works through shader instances:

1. It polls the active texture set every 150 ms (there is no selection event in the API).
2. On a change it reads the shader-instance assignment (`alg.shaders.shaderInstancesToObject()`), assigns a
   "TexSet Spotlight" instance of `texset_spotlight_shader.glsl` to the selected set and, with X-ray on, a
   "TexSet Spotlight Ghost" instance to every other set (`shaderInstancesFromObject()`).
3. The shader is the stock metal/rough + alpha-blending PBR with extra parameters: `highlight_mix` /
   `highlight_color` / `highlight_intensity` / `highlight_tint` (emissive flash) and `ghost_alpha` / `ghost_dim`
   (x-ray). The plugin animates them with `alg.shaders.setParameters()`.
4. When the fade ends the original instances are put back and the temporary instances are removed.

## Limitations

- The flash marks the project as modified (shader assignments are part of the project), even though they are restored.
- Painter shaders cannot disable the depth test, so the highlighted set is not drawn "on top"; X-ray makes the
  others transparent instead.
- Texture sets that use a custom shader instance are rendered with the stock PBR look for the two seconds of the flash.
- Painter removes a shader instance that no texture set uses any more; the plugin keeps a copy of the instance
  definitions and re-creates them on restore.

## License

MIT. Copyright (c) 2026 Kurakurage / KURAKU.lab.

---

## 日本語

テクスチャセットリストでテクスチャセットを選ぶと、そのセットが割り当たっているメッシュが 3D ビューで光って消える
Substance 3D Painter 用プラグインです。「X-ray」をオンにすると、光っている間ほかのセットが透けて暗くなるので、
機体の中に隠れた部品も確認できます。

- 選んだとき 1 秒光り、1 秒かけて消える（時間は変えられる）
- X-ray: ほかのセットを不透明度 12 %・明るさ 35 % にする（変えられる・切れる）
- レイヤーは触らない。光り終わったとき・保存や閉じる前・途中で落ちたときの次回起動時に、元のシェーダーの割り当てへ戻す
- メニューバーの「Spotlight」: 有効/無効、X-ray、今すぐ光らせる、色、表示時間、消える時間、強さ、X-ray の不透明度・明るさ

### 入れ方
`texset_spotlight.py` と `texset_spotlight_shader.glsl` を Painter のプラグインフォルダー
（Windows: `ドキュメント\Adobe\Adobe Substance 3D Painter\python\plugins\`）にコピーして Painter を起動する。

### 動作を確認した環境
- Adobe Substance 3D Painter **12.1.1**（Vulkan）、Python API 0.3.5（Python 3.13.9・PySide6 6.8.6）、JavaScript API 1.1.20
- Windows 11 / NVIDIA GeForce RTX 5060 Ti、テクスチャセット 40 個（1024²）のプロジェクト
- 12.x のほかの版でも動く見込みだが未確認。11 以前（PySide2）はそのままでは動かない

### 仕組みと制限
ビューに色を重ねる API が無いため、選んだセットだけを一時的に発光付きシェーダーのインスタンスに切り替え、
`highlight_mix` を 1 → 0 に動かしてから元に戻しています（JS API `alg.shaders`）。
光るたびにプロジェクトに「未保存」の印が付きます（割り当ては元に戻っています）。Painter のシェーダーは深度テストを
切れないので「手前に描く」ことはできず、代わりに周りを透かしています。独自シェーダーを使うセットは光っている 2 秒だけ
標準の PBR の見え方になります。
