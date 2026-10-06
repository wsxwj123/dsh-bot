// 假作息查询（代替 python3 hang_situation.py <bot> --plan）：原样输出测试写好的 JSON 文件；没有文件就当查不到。
import { existsSync, readFileSync } from 'fs'
const f = process.argv[2]!
if (!existsSync(f)) process.exit(1)
process.stdout.write(readFileSync(f, 'utf8').trim() + '\n')
