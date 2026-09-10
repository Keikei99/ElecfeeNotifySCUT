// Node 包装：复用 CAS 原版 des.js 的 strEnc，避免手工移植 DES 出错。
// 用法: node src/strenc.js "<data>" "<k1>" "<k2>" "<k3>"
const fs = require('fs');
const path = require('path');
const code = fs.readFileSync(path.join(__dirname, 'des.js'), 'utf8');
// des.js 定义了全局函数 strEnc/getKeyBytes 等，直接在本作用域内 eval 引入
eval(code);
const [, , data, k1, k2, k3] = process.argv;
process.stdout.write(strEnc(data, k1, k2, k3));
