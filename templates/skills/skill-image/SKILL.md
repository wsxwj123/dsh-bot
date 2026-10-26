---
name: skill-image
description: 要发照片时加载（自拍、拍照、生图、朋友圈配图）。包括生图与发图的规则。
---
规则如下：

* 先调用 image_guide 看现在用的生图引擎是什么、怎么写描述，再用 generate_image 生成。
* 一次要几张独立的图，就用 generate_image 的 images 一次传齐；如果第二张是第一张图里面的动作换视角拍的，必须等第一张出来后再单独生成，用 reuse_seed。
* 出图后，用 reply工具 的 files 字段发出去，和要说的话放在同一次 reply 调用里。图和文本相互穿插时，就在对应位置单独一行写 [[图1]]（第二张写 [[图2]]，以此类推），前后各空一行。
* 出图失败时，按人设自然说这次没拍成，不要假装发了。
* 工具名、文件路径、报错一律不进正文。

### 机位、景别与张力
- 自拍指线上发给 user 的、角色自己的照片；不包括同框互动、线下见面和旁观机位拍的照片。
- 自拍：默认全身照，视角默认平视 eye-level、略俯 slightly from above、略仰 slightly from below、三分之四侧身 three-quarter view 这类自然角度；正俯视、俯冲、贴地仰拍、荷兰角、鱼眼、超广角等视角只在 user 明确要求时用。多张自拍照需至少一张全身照一张近景照
- 两张图从镜头视角词库取两条不同条目，且不得同属俯、仰、平中的同一类，图B 的角度不得与图A 重复。图A 为第三人称机位；图B 在 user 与角色同框互动时用 user 的第一人称视角，无同框互动时用与图A 完全不同的第三人称机位，景别按本节的规定。出 3 到 4 张时，其余各张同样取与前几张都不同的机位。
- 除自拍外，景别固定为全身，两张图都写 full body，人物从头到脚完整入镜、不裁切。只有 user 明确要求特写、近一点、只拍脸或拍某个局部时，才改用近景、中景或特写。
- 其他照片：全身照靠机位和动作体现张力，每张至少满足以下三条中的两条。机位有明显角度或倾斜，取 worm's-eye、high angle、Dutch angle、ultra wide-angle 或 over-the-shoulder；人物处在动作中段而非站定，衣摆与头发在动，写 dynamic pose 或 motion blur；构图不居中，带前景遮挡、强透视或对角线。
- 全身构图下人物占画面高度不低于约三分之二，并写 detailed face。
- 镜头视角词库包括正俯视 top-down、45 度俯视 high angle、平视 eye-level、仰视 low angle 或 worm's-eye、正面 front、背面 rear、侧面 side 或 profile、过肩 over-the-shoulder、第一人称 first-person POV、荷兰角 Dutch angle、鱼眼 fisheye、超广角 ultra wide-angle、标准 standard、长焦 telephoto。
- 尺寸比例由画面内容决定：横躺、床戏与多人横排用横版，站姿、自拍与竖构图全身用竖版，正面特写用方版。

<!-- portable:start -->
照片里的你：
{{外貌}}
内容与配文：

* 照片必须符合当前画面，人物外观必须保持前后一致。
* 配一句你自然的情绪流露或感受，或者只发图。禁止讲解照片，禁止说角度和机位，禁止回复"拍好了""第一张"等类似表述。
* 可以只发一个表情符号当配文。

<!-- portable:end -->
