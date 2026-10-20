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
  cancelled: "idle",
};

export const BILL_STATUS = {
  consistent: { label: "一致", className: "ready" },
  inconsistent: { label: "不一致", className: "failed" },
  incomplete: { label: "信息不全", className: "review" },
};

export const FIELD_STATUS = {
  match: { label: "一致", className: "ready" },
  similar: { label: "近似", className: "review" },
  mismatch: { label: "差异", className: "failed" },
  missing_in_manifest: { label: "舱单无值", className: "review" },
  missing_in_house_bill: { label: "分单无值", className: "review" },
  missing_both: { label: "双方无值", className: "idle" },
};

export const REASON_LABEL = {
  mismatch: "差异",
  missing: "缺失",
  similar: "近似",
  low_confidence: "置信度",
};

export const KIND_LABEL = {
  master_bill: "主单",
  house_bill: "分单",
  manifest: "舱单",
  other: "其他",
};

export const NODES = [
  "解析文档",
  "图像转写",
  "识别主单/分单/舱单",
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

// 分单字段（与后端 HOUSE_FIELD_SPECS 一致，38 项）
export const DEFAULT_HOUSE_FIELDS = [
  ["hawb_no", "分单号", "shipment"],
  ["origin_port", "始发港", "shipment"],
  ["dest_port", "目的港", "shipment"],
  ["flight_no", "航班号", "shipment"],
  ["flight_date", "航班日期", "shipment"],
  ["pieces", "件数", "shipment"],
  ["weight", "重量", "shipment"],
  ["volume", "体积", "shipment"],
  ["first_carrier_code", "航班二字码（By First Carrier）", "shipment"],
  ["currency", "币种（Currency）", "shipment"],
  ["shipper_name", "发货人名称", "shipper"],
  ["shipper_address", "发货人地址", "shipper"],
  ["shipper_city", "发货人城市", "shipper"],
  ["shipper_postal_code", "发货人邮编", "shipper"],
  ["shipper_state", "发货人州名", "shipper"],
  ["shipper_contact", "发货人联系人", "shipper"],
  ["shipper_phone", "发货人手机号", "shipper"],
  ["shipper_email", "发货人邮箱", "shipper"],
  ["shipper_fax", "发货人传真", "shipper"],
  ["consignee_name", "收货人名称", "consignee"],
  ["consignee_address", "收货人地址", "consignee"],
  ["consignee_city", "收货人城市", "consignee"],
  ["consignee_postal_code", "收货人邮编", "consignee"],
  ["consignee_state", "收货人州名", "consignee"],
  ["consignee_contact", "收货人联系人", "consignee"],
  ["consignee_phone", "收货人手机号", "consignee"],
  ["consignee_email", "收货人邮箱", "consignee"],
  ["consignee_fax", "收货人传真", "consignee"],
  ["declared_value_carriage", "声明价值（Declared Value for Carriage）", "declaration"],
  ["handling_info", "操作信息（Handling Information）", "declaration"],
  ["rcp_pieces", "制单件数（No. of Pieces RCP）", "cargo"],
  ["rcp_gross_weight", "制单重量（Gross Weight）", "cargo"],
  ["rate_class", "运价类别（Rate Class）", "cargo"],
  ["chargeable_weight", "计费重量（Chargeable Weight）", "cargo"],
  ["rate_charge", "单价（Rate/Charge）", "cargo"],
  ["rate_total", "总价（Rate/Total）", "cargo"],
  ["rcp_volume", "制单体积（Volume）", "cargo"],
  ["goods_description", "品名（Nature and Quantity of Goods）", "cargo"],
].map(([key, label, group]) => ({ key, label, group }));

// 舱单与分单共用同一套字段，便于逐字段核对
export const DEFAULT_MANIFEST_FIELDS = DEFAULT_HOUSE_FIELDS;
export const DEFAULT_FIELDS = DEFAULT_HOUSE_FIELDS;

// 分单固定单证模板：严格按参考 AWB 版面从上到下、从左到右排列，
// 只保留需要抽取的字段；col/span 为 12 栅格中的列位置。
const PARTY_TEMPLATE_FIELDS = [
  { key: "name", span: 12, label: "Name" },
  { key: "address", span: 12, textarea: true, label: "Address" },
  { key: "city", span: 3, label: "City" },
  { key: "postal_code", span: 3, label: "Postal Code" },
  { key: "state", span: 3, label: "State" },
  { key: "contact", span: 3, label: "Contact" },
  { key: "phone", span: 4, label: "Mobile" },
  { key: "email", span: 4, label: "Email" },
  { key: "fax", span: 4, label: "Fax" },
];

export const HOUSE_TEMPLATE = [
  // 右上：HAWB No.；左侧空白放单据摘要信息
  { id: "hawb", info: true, fields: [{ key: "hawb_no", col: 9, span: 4, label: "HAWB No." }] },
  // 当事人整行：名称 / 地址（多行）/ 城市 + 国家 + 电话 + 邮箱
  { id: "shipper", party: "shipper", col: 1, span: 12, fields: PARTY_TEMPLATE_FIELDS },
  { id: "consignee", party: "consignee", col: 1, span: 12, fields: PARTY_TEMPLATE_FIELDS },
  // 运输与申报：Airport of Departure 右侧依次是 By First Carrier / Currency / Declared Value
  {
    id: "routing",
    fields: [
      { key: "origin_port", col: 1, span: 3, label: "Airport of Departure" },
      { key: "first_carrier_code", col: 4, span: 2, label: "By First Carrier" },
      { key: "currency", col: 6, span: 2, label: "Currency" },
      {
        key: "declared_value_carriage",
        col: 8,
        span: 5,
        label: "Declared Value for Carriage",
      },
    ],
  },
  // Airport of Destination + Requested Flight/Date，整行三等分
  {
    id: "flight",
    fields: [
      { key: "dest_port", col: 1, span: 4, label: "Airport of Destination" },
      { key: "flight_no", col: 5, span: 4, label: "Flight No." },
      { key: "flight_date", col: 9, span: 4, date: true, label: "Flight Date" },
    ],
  },
  // Handling Information：整行多行输入
  {
    id: "handling",
    fields: [
      { key: "handling_info", col: 1, span: 12, label: "Handling Information", textarea: true },
    ],
  },
  // 货物表：No. of Pieces RCP → Gross Weight → Rate Class → Chargeable Weight → Volume → Nature and Quantity
  // 该行用 24 栅格细分列宽，件数不至于过窄
  {
    id: "cargo",
    cols: 24,
    fields: [
      { key: "rcp_pieces", col: 1, span: 2, label: "No. of Pieces RCP" },
      { key: "rcp_gross_weight", col: 3, span: 3, label: "Gross Weight" },
      { key: "rate_class", col: 6, span: 3, label: "Rate Class" },
      { key: "chargeable_weight", col: 9, span: 3, label: "Chargeable Weight" },
      { key: "rate_charge", col: 12, span: 3, label: "Rate/Charge" },
      { key: "rate_total", col: 15, span: 3, label: "Rate/Total" },
      { key: "rcp_volume", col: 18, span: 2, label: "Volume" },
      { key: "goods_description", col: 20, span: 5, label: "Nature and Quantity of Goods" },
    ],
  },
];

export const MANIFEST_GROUPS = [
  "shipment",
  "shipper",
  "consignee",
  "declaration",
  "cargo",
];

// 舱单页签字段名沿用舱单表格里的英文表头
export const MANIFEST_LABELS_EN = {
  hawb_no: "HAWB No.",
  origin_port: "Origin",
  dest_port: "Destination",
  flight_no: "Flight No.",
  flight_date: "Flight Date",
  pieces: "Pieces",
  weight: "Weight",
  volume: "Volume",
  first_carrier_code: "Carrier",
  currency: "Currency",
  shipper_name: "Shipper Name",
  shipper_address: "Shipper Address",
  shipper_city: "Shipper City",
  shipper_postal_code: "Shipper Postal",
  shipper_state: "Shipper State",
  shipper_contact: "Shipper Contact",
  shipper_phone: "Shipper Tel",
  shipper_email: "Shipper Email",
  shipper_fax: "Shipper Fax",
  consignee_name: "Consignee Name",
  consignee_address: "Consignee Address",
  consignee_city: "Consignee City",
  consignee_postal_code: "Consignee Postal",
  consignee_state: "Consignee State",
  consignee_contact: "Consignee Contact",
  consignee_phone: "Consignee Tel",
  consignee_email: "Consignee Email",
  consignee_fax: "Consignee Fax",
  declared_value_carriage: "Declared Value",
  handling_info: "Handling Info",
  rcp_pieces: "Pieces RCP",
  rcp_gross_weight: "Gross Weight",
  rate_class: "Rate Class",
  chargeable_weight: "Chargeable Weight",
  rate_charge: "Rate/Charge",
  rate_total: "Total",
  rcp_volume: "Volume",
  goods_description: "Goods Description",
};

// 单证上需要人工填写、不参与识别的栏位（当前全部改为自动抽取）
export const HOUSE_MANUAL_KEYS = [];

// 分单表单必填项（提交前校验，只校验模板中可见的栏位）
export const REQUIRED_KEYS = [
  "hawb_no",
  "origin_port",
  "dest_port",
  "flight_no",
  "flight_date",
];

export const MANIFEST_REQUIRED_KEYS = ["hawb_no", "pieces", "weight"];
