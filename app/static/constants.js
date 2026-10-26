export const STATUS_LABEL = {
  queued: "排队中",
  running: "分析中",
  paused: "已暂停",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export const STATUS_CLASS = {
  queued: "idle",
  running: "running",
  paused: "review",
  succeeded: "ready",
  failed: "failed",
  // 已取消是用户主动操作，用中性灰与「失败」区分（与 DocMind 一致）
  cancelled: "cancelled",
};

export const BILL_STATUS = {
  consistent: { label: "一致", className: "ready" },
  inconsistent: { label: "不一致", className: "failed" },
  incomplete: { label: "信息不全", className: "review" },
};

export const REASON_LABEL = {
  mismatch: "差异",
  missing: "缺失",
  similar: "近似",
  low_confidence: "置信度",
};

export const NODES = [
  "解析文档",
  "图像转写",
  "识别主单/分单/舱单",
  // 只对分单页做四象限放大转写；没有分单时图会跳过该节点
  "分单局部精读",
  "抽取分单与舱单字段",
  "核对分单与舱单",
  "生成结果",
];

export const GROUP_LABEL = {
  shipment: "运输信息",
  shipper: "发货人 SHIPPER",
  consignee: "收货人 CONSIGNEE",
  declaration: "申报与保险",
  cargo: "制单与货物",
};

export const PARTY_GROUP_LABEL = {
  shipper: "SHIPPER'S NAME AND ADDRESS",
  consignee: "CONSIGNEE'S NAME AND ADDRESS",
};

// 分单字段（与后端 HOUSE_FIELD_SPECS 一致，36 项；key 为接口字段名）
export const DEFAULT_HOUSE_FIELDS = [
  ["hawb", "分单号", "shipment"],
  ["sfg", "始发港", "shipment"],
  ["mdg", "目的港", "shipment"],
  ["hbh", "航班号", "shipment"],
  ["hbrq", "航班日期", "shipment"],
  ["currency", "币种（Currency）", "shipment"],
  ["companytitle_fhr_hawb", "发货人名称", "shipper"],
  ["address_fhr_hawb", "发货人地址", "shipper"],
  ["city_fhr_hawb", "发货人城市", "shipper"],
  ["postcode_fhr_hawb", "发货人邮编", "shipper"],
  ["companycode_fhr_hawb", "发货人国家二字码", "shipper"],
  ["state_fhr_hawb", "发货人州名", "shipper"],
  ["lxr_fhr_hawb", "发货人联系人", "shipper"],
  ["phone_fhr_hawb", "发货人手机号", "shipper"],
  ["email_fhr_hawb", "发货人邮箱", "shipper"],
  ["fax_fhr_hawb", "发货人传真", "shipper"],
  ["companytitle_shr_hawb", "收货人名称", "consignee"],
  ["address_shr_hawb", "收货人地址", "consignee"],
  ["city_shr_hawb", "收货人城市", "consignee"],
  ["postcode_shr_hawb", "收货人邮编", "consignee"],
  ["companycode_shr_hawb", "收货人国家二字码", "consignee"],
  ["state_shr_hawb", "收货人州名", "consignee"],
  ["lxr_shr_hawb", "收货人联系人", "consignee"],
  ["phone_shr_hawb", "收货人手机号", "consignee"],
  ["email_shr_hawb", "收货人邮箱", "consignee"],
  ["fax_shr_hawb", "收货人传真", "consignee"],
  ["declarecustom", "申报价值（Declared Value for Customs）", "declaration"],
  ["handlinginfo", "操作信息（Handling Information）", "declaration"],
  ["zdpiece", "制单件数（No. of Pieces RCP）", "cargo"],
  ["zdweight", "制单重量（Gross Weight）", "cargo"],
  ["ratelevel", "运价类别（Rate Class）", "cargo"],
  ["jfweight", "计费重量（Chargeable Weight）", "cargo"],
  ["rate", "单价（Rate/Charge）", "cargo"],
  ["ratetotal", "总价（Rate/Total）", "cargo"],
  ["zdvolume", "制单体积（Volume）", "cargo"],
  ["pm", "品名（Nature and Quantity of Goods）", "cargo"],
].map(([key, label, group]) => ({ key, label, group }));

// 舱单只抽取核对字段：分单号 + 目的港 + 制单件数/重量/计费重/体积
export const DEFAULT_MANIFEST_FIELDS = [
  ["hawb_no", "分单号", "shipment"],
  ["dest_port", "目的港", "shipment"],
  ["pieces", "件数", "shipment"],
  ["weight", "重量", "shipment"],
  ["chargeable_weight", "计费重量", "cargo"],
  ["volume", "体积", "shipment"],
].map(([key, label, group]) => ({ key, label, group }));

// 参与人字段 → 接口字段名：shipper → *_fhr_hawb，consignee → *_shr_hawb
const PARTY_FIELD_PREFIX = {
  name: "companytitle",
  address: "address",
  city: "city",
  postal_code: "postcode",
  country: "companycode",
  state: "state",
  contact: "lxr",
  phone: "phone",
  email: "email",
  fax: "fax",
};
const PARTY_CODE = { shipper: "fhr", consignee: "shr" };

export function partyFieldKey(party, name) {
  return `${PARTY_FIELD_PREFIX[name] || name}_${PARTY_CODE[party] || party}_hawb`;
}

// 分单固定单证模板：严格按参考 AWB 版面从上到下、从左到右排列，
// 只保留需要抽取的字段；col/span 为 12 栅格中的列位置。
// 12 栅格：第一行 5 个（城市/邮编/国家/州/联系人 = 3+2+2+2+3），第二行 3 个（电话/邮箱/传真各 4）
const PARTY_TEMPLATE_FIELDS = [
  { key: "name", span: 12, label: "Name" },
  { key: "address", span: 12, textarea: true, label: "Address" },
  { key: "city", span: 3, label: "City" },
  { key: "postal_code", span: 2, label: "Postal Code" },
  { key: "country", span: 2, label: "Country" },
  { key: "state", span: 2, label: "State" },
  { key: "contact", span: 3, label: "Contact" },
  { key: "phone", span: 4, label: "Mobile" },
  { key: "email", span: 4, label: "Email" },
  { key: "fax", span: 4, label: "Fax" },
];

export const HOUSE_TEMPLATE = [
  // 右上：HAWB No.；左侧空白放单据摘要信息
  { id: "hawb", info: true, fields: [{ key: "hawb", col: 9, span: 4, label: "HAWB No." }] },
  // 当事人整行：名称 / 地址（多行）/ 城市 + 国家 + 电话 + 邮箱
  { id: "shipper", party: "shipper", col: 1, span: 12, fields: PARTY_TEMPLATE_FIELDS },
  { id: "consignee", party: "consignee", col: 1, span: 12, fields: PARTY_TEMPLATE_FIELDS },
  // 运输与申报：Airport of Departure 右侧依次是 Currency / Declared Value（不再抽 By First Carrier）
  {
    id: "routing",
    fields: [
      { key: "sfg", col: 1, span: 4, label: "Airport of Departure" },
      { key: "currency", col: 5, span: 3, label: "Currency" },
      {
        key: "declarecustom",
        col: 8,
        span: 5,
        label: "Declared Value for Customs",
      },
    ],
  },
  // Airport of Destination + Requested Flight/Date，整行三等分
  {
    id: "flight",
    fields: [
      { key: "mdg", col: 1, span: 4, label: "Airport of Destination" },
      { key: "hbh", col: 5, span: 4, label: "Flight No." },
      { key: "hbrq", col: 9, span: 4, date: true, label: "Flight Date" },
    ],
  },
  // Handling Information：整行多行输入
  {
    id: "handling",
    fields: [
      { key: "handlinginfo", col: 1, span: 12, label: "Handling Information", textarea: true },
    ],
  },
  // 货物表两排、每排 4 个：第一排 件数 / 毛重 / 运价 / 计费重
  {
    id: "cargo",
    fields: [
      { key: "zdpiece", col: 1, span: 3, label: "No. of Pieces RCP" },
      { key: "zdweight", col: 4, span: 3, label: "Gross Weight" },
      { key: "ratelevel", col: 7, span: 3, label: "Rate Class" },
      { key: "jfweight", col: 10, span: 3, label: "Chargeable Weight" },
    ],
  },
  // 第二排 单价 / 总价 / 体积 / 品名（品名占半行，保证长名称能完整显示）
  {
    id: "cargo2",
    fields: [
      { key: "rate", col: 1, span: 2, label: "Rate/Charge" },
      { key: "ratetotal", col: 3, span: 2, label: "Rate/Total" },
      { key: "zdvolume", col: 5, span: 2, label: "Volume" },
      {
        key: "pm",
        col: 7,
        span: 6,
        label: "Nature and Quantity of Goods",
      },
    ],
  },
];

export const MANIFEST_GROUPS = ["shipment", "cargo"];

// 舱单页签字段名沿用舱单表格里的英文表头
export const MANIFEST_LABELS_EN = {
  hawb_no: "HAWB No.",
  dest_port: "Destination",
  pieces: "Pieces",
  weight: "Weight",
  chargeable_weight: "Chargeable Weight",
  volume: "Volume",
};

// 单证上需要人工填写、不参与识别的栏位（当前全部改为自动抽取）
export const HOUSE_MANUAL_KEYS = [];

// 分单表单必填项：除发货人/收货人信息外的所有字段都必须有值
export const REQUIRED_KEYS = DEFAULT_HOUSE_FIELDS.filter(
  (field) => field.group !== "shipper" && field.group !== "consignee"
).map((field) => field.key);

export const MANIFEST_REQUIRED_KEYS = ["hawb_no", "pieces", "weight"];
