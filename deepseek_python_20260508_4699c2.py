import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from io import BytesIO
import base64
import re
from datetime import datetime

# 設定頁面
st.set_page_config(page_title="審計抽樣工具 (含實質性分析)", layout="wide")
st.title("📊 審計抽樣小程式 - 結合實質性分析程序 (SAP)")
st.markdown("適用於總分類帳 (GL) 超過五萬筆資料，無需同期數據。")

# 初始化 session state
for _key in ["sampled_df", "raw_df", "sap_result", "cleaned_df", "cleaned_meta",
             "_gl_clean_stage1", "_gl_clean_final", "_gl_clean_meta"]:
    if _key not in st.session_state:
        st.session_state[_key] = None

# ─── GL 清理輔助函式 ────────────────────────────────────────────────────────

def detect_header_row(raw_df, max_scan=20):
    """Return row index with the most non-empty string cells (most likely the real header)."""
    best_row, best_count = 0, 0
    for i in range(min(max_scan, len(raw_df))):
        count = raw_df.iloc[i].apply(lambda v: isinstance(v, str) and v.strip() != '').sum()
        if count > best_count:
            best_count, best_row = count, i
    return best_row

def drop_blank_columns(df, threshold=0.8):
    """Drop columns where more than threshold fraction of values are NaN or empty."""
    keep = [c for c in df.columns
            if df[c].apply(lambda v: pd.isna(v) or str(v).strip() == '').mean() < threshold]
    return df[keep]

def forward_fill_merged(df):
    """Forward-fill NaN values row-wise to resolve merged-cell artifacts."""
    return df.ffill(axis=0)

def clean_amount_series(series):
    """Strip currency symbols/commas, handle (1,234.56) → -1234.56, coerce to float."""
    _sym = re.compile(r'[NT$HK$¥$£€,\s]')
    _non_num = re.compile(r'[^\d.\-]')

    def _clean(v):
        if pd.isna(v):
            return v
        s = str(v).strip()
        negative = s.startswith('(') and s.endswith(')')
        if negative:
            s = s[1:-1]
        s = _non_num.sub('', _sym.sub('', s))
        try:
            result = float(s)
            return -result if negative else result
        except ValueError:
            return None

    return series.apply(_clean)

def combine_debit_credit(df, debit_col, credit_col):
    """Combine separate Debit / Credit columns into a single signed '金額' column."""
    _sym = re.compile(r'[NT$HK$¥$£€,\s]')
    _non_num = re.compile(r'[^\d.\-]')

    def _to_float(v):
        if pd.isna(v):
            return 0.0
        s = _non_num.sub('', _sym.sub('', str(v).strip()))
        try:
            return float(s)
        except ValueError:
            return 0.0

    df = df.copy()
    df['金額'] = df[debit_col].apply(_to_float) - df[credit_col].apply(_to_float)
    return df

def remove_noise_rows(df, voucher_col):
    """Remove rows with blank voucher or subtotal/total keyword patterns."""
    noise = re.compile(r'合計|小計|總計|total|grand|sub.?total', re.IGNORECASE)
    mask_blank = df[voucher_col].apply(lambda v: pd.isna(v) or str(v).strip() == '')
    mask_noise = df[voucher_col].astype(str).str.contains(noise, na=False)
    return df[~(mask_blank | mask_noise)].reset_index(drop=True)

# ─── Tab 版面配置 ────────────────────────────────────────────────────────────

tab0, tab1 = st.tabs(["📋 Step 0: GL Excel 清理", "📊 Step 1~2: SAP + 抽樣"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 0: GL EXCEL 清理
# ══════════════════════════════════════════════════════════════════════════════
with tab0:
    st.header("GL Excel 清理工具")
    st.markdown(
        "上傳原始 GL 檔案，自動處理多餘標題列、空白欄、合併儲存格、借貸分欄等問題，"
        "輸出可直接供 SAP 抽樣工具使用的整潔資料。"
    )

    raw_file = st.file_uploader(
        "上傳原始 GL 檔案 (Excel 或 CSV)",
        type=["xlsx", "xls", "csv"],
        key="cleaner_upload"
    )

    if raw_file is not None:
        # Load without assuming any header
        if raw_file.name.lower().endswith('.csv'):
            raw_df = pd.read_csv(raw_file, header=None, dtype=str)
        else:
            raw_df = pd.read_excel(raw_file, header=None, dtype=str)

        st.subheader("原始資料預覽 (前 15 列)")
        preview_df = raw_df.head(15).copy()
        preview_df.index = range(1, len(preview_df) + 1)  # show 1-based row numbers
        st.dataframe(preview_df, use_container_width=True)

        auto_header = detect_header_row(raw_df) + 1  # convert to 1-based for display
        header_row = st.number_input(
            f"第幾行為欄位標題？(自動偵測第 {auto_header} 行，可手動調整；資料將從標題行下一行開始讀取)",
            min_value=1,
            max_value=min(31, len(raw_df)),
            value=int(auto_header),
            step=1
        )
        st.caption(f"目前設定：第 {int(header_row)} 行為標題，第 {int(header_row)+1} 行起為資料")

        if st.button("套用清理設定", type="primary", key="btn_stage1"):
            raw_file.seek(0)
            pandas_header = int(header_row) - 1  # convert 1-based UI to 0-based pandas
            if raw_file.name.lower().endswith('.csv'):
                df_s1 = pd.read_csv(raw_file, header=pandas_header, dtype=str)
            else:
                df_s1 = pd.read_excel(raw_file, header=pandas_header, dtype=str)

            df_s1.columns = [str(c).strip() for c in df_s1.columns]
            df_s1 = drop_blank_columns(df_s1)
            df_s1 = forward_fill_merged(df_s1)
            st.session_state._gl_clean_stage1 = df_s1
            st.session_state._gl_clean_final = None  # reset downstream
            st.success(f"初步清理完成：{len(df_s1):,} 列 × {len(df_s1.columns)} 欄")

    if st.session_state._gl_clean_stage1 is not None:
        df_s1 = st.session_state._gl_clean_stage1
        cols = list(df_s1.columns)

        st.subheader("欄位設定")
        voucher_col_c = st.selectbox("憑證編號 / 傳票號欄 (用於移除合計列)", cols, key="c_voucher")
        debit_credit_mode = st.checkbox("借貸分欄模式 (Debit / Credit 各一欄)", key="c_dc_mode")

        if debit_credit_mode:
            debit_col_c = st.selectbox("借方金額欄 (Debit)", cols, key="c_debit")
            credit_col_c = st.selectbox("貸方金額欄 (Credit)", cols, key="c_credit")
            amount_col_c = "金額"
        else:
            amount_col_c = st.selectbox("金額欄 (單一欄位)", cols, key="c_amount")

        date_col_c = st.selectbox("記帳日期欄", cols, key="c_date")
        acct_opts = ["無"] + cols
        account_col_c = st.selectbox("科目欄 (選填)", acct_opts, key="c_account")

        if st.button("執行欄位清理 & 移除雜訊列", type="primary", key="btn_stage2"):
            result = df_s1.copy()

            if debit_credit_mode:
                result = combine_debit_credit(result, debit_col_c, credit_col_c)
            else:
                result[amount_col_c] = clean_amount_series(result[amount_col_c])

            result = remove_noise_rows(result, voucher_col_c)

            st.session_state._gl_clean_final = result
            st.session_state._gl_clean_meta = {
                "voucher_col": voucher_col_c,
                "amount_col": amount_col_c,
                "date_col": date_col_c,
                "account_col": account_col_c,
            }
            st.success(f"清理完成！剩餘 {len(result):,} 筆資料")
            st.dataframe(result.head(20), use_container_width=True)
            null_count = result[amount_col_c].isna().sum()
            st.write(
                f"金額欄型態：`{result[amount_col_c].dtype}` — "
                f"無法解析筆數：{null_count}"
            )

    if st.session_state._gl_clean_final is not None:
        final_clean = st.session_state._gl_clean_final
        meta_c = st.session_state._gl_clean_meta

        ca, cb = st.columns(2)
        with ca:
            out = BytesIO()
            with pd.ExcelWriter(out, engine='openpyxl') as writer:
                final_clean.to_excel(writer, sheet_name='GL_已清理', index=False)
            out.seek(0)
            b64 = base64.b64encode(out.read()).decode()
            href = (
                f'<a href="data:application/vnd.openxmlformats-officedocument.'
                f'spreadsheetml.sheet;base64,{b64}" download="gl_cleaned.xlsx">'
                f'📥 下載清理後 Excel</a>'
            )
            st.markdown(href, unsafe_allow_html=True)

        with cb:
            if st.button("➡️ 傳送並前往 Step 1~2", type="primary", key="btn_send"):
                st.session_state.cleaned_df = final_clean
                st.session_state.cleaned_meta = meta_c
                # Auto-switch to Tab 1 via JS
                components.html(
                    """<script>
                    var tabs = window.parent.document.querySelectorAll('[data-baseweb="tab"]');
                    if (tabs.length > 1) tabs[1].click();
                    </script>""",
                    height=0
                )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: SAP + 抽樣 (原有邏輯，最小修改)
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    with st.sidebar:
        st.header("1. 上傳 GL 檔案")

        df_loaded = False

        if st.session_state.cleaned_df is not None:
            # ── 從清理工具傳入的資料 ──
            meta = st.session_state.cleaned_meta or {}
            st.success(f"✅ 已從清理工具載入 {len(st.session_state.cleaned_df):,} 筆資料")
            if st.button("清除並重新上傳"):
                st.session_state.cleaned_df = None
                st.session_state.cleaned_meta = None
                st.session_state.sap_result = None
                st.rerun()
            df = st.session_state.cleaned_df.copy()

            def _idx(cols, key):
                val = meta.get(key, '')
                lst = list(cols)
                return lst.index(val) if val and val in lst else 0

            st.subheader("欄位對應")
            col_voucher = st.selectbox("憑證編號欄 (唯一識別)", df.columns,
                                       index=_idx(df.columns, 'voucher_col'))
            col_amount  = st.selectbox("金額欄 (數值)", df.columns,
                                       index=_idx(df.columns, 'amount_col'))
            col_date    = st.selectbox("記帳日期欄", df.columns,
                                       index=_idx(df.columns, 'date_col'))
            _ao = ["無"] + list(df.columns)
            _av = meta.get('account_col', '無')
            col_account = st.selectbox("科目欄 (選填，用於分層)", _ao,
                                       index=_ao.index(_av) if _av in _ao else 0)
            df_loaded = True

        else:
            # ── 直接上傳 ──
            uploaded_file = st.file_uploader("上傳 Excel 或 CSV", type=["xlsx", "csv"])

            if uploaded_file is not None:
                tab1_header_row = st.number_input(
                    "第幾行為欄位標題？(預設第 1 行，資料從下一行開始讀取)",
                    min_value=1, max_value=30, value=1, step=1,
                    key="tab1_header_row"
                )
                _h = int(tab1_header_row) - 1  # convert to 0-based
                if uploaded_file.name.endswith('.csv'):
                    df = pd.read_csv(uploaded_file, header=_h)
                else:
                    df = pd.read_excel(uploaded_file, header=_h)

                st.success(f"成功上傳 {len(df)} 筆資料 (標題為第 {int(tab1_header_row)} 行)")

                # ── 借貸分欄模式 ──
                tab1_dc_mode = st.checkbox("借貸分欄模式 (Debit / Credit 各一欄)", key="tab1_dc_mode")
                if tab1_dc_mode:
                    tab1_debit_col  = st.selectbox("借方金額欄 (Debit)",  df.columns, key="tab1_debit")
                    tab1_credit_col = st.selectbox("貸方金額欄 (Credit)", df.columns, key="tab1_credit")
                    df = combine_debit_credit(df, tab1_debit_col, tab1_credit_col)
                    st.caption("已合併為「金額」欄 (借方 − 貸方)")

                # ── 科目名稱向下填滿（Sage 50 / PeachTree 等格式）──
                tab1_filldown = st.checkbox(
                    "科目名稱向下填滿（Sage 50 / PeachTree 格式）",
                    key="tab1_filldown",
                    help="適用於科目名稱只印在第一筆交易，其餘列留空的 GL 格式"
                )
                if tab1_filldown:
                    tab1_filldown_col = st.selectbox(
                        "選擇要向下填滿的欄位（通常為科目代號或科目名稱欄）",
                        df.columns, key="tab1_filldown_col"
                    )
                    df[tab1_filldown_col] = (
                        df[tab1_filldown_col]
                        .replace('', pd.NA)
                        .ffill()
                    )
                    st.caption(f"已向下填滿「{tab1_filldown_col}」欄")

                st.subheader("欄位對應")
                col_voucher = st.selectbox("憑證編號欄 (唯一識別)", df.columns)
                _amt_cols   = list(df.columns)
                _amt_def    = _amt_cols.index("金額") if "金額" in _amt_cols else 0
                col_amount  = st.selectbox("金額欄 (數值)", df.columns, index=_amt_def)
                col_date    = st.selectbox("記帳日期欄", df.columns)
                col_account = st.selectbox("科目欄 (選填，用於分層)", ["無"] + list(df.columns))
                df_loaded = True

        if df_loaded:
            st.subheader("審計參數設定")
            materiality = st.number_input(
                "重大性門檻金額 (超過者強制全查)",
                min_value=0.0, value=100000.0, step=10000.0
            )
            _SAP_LEVELS = {"高（±1σ，較嚴格）": 1.0, "中（±2σ，標準）": 2.0, "低（±3σ，較寬鬆）": 3.0, "無（停用 SAP）": None}
            sap_level_label = st.selectbox(
                "SAP 異常靈敏度",
                options=list(_SAP_LEVELS.keys()),
                index=1
            )
            sap_std_multiplier = _SAP_LEVELS[sap_level_label]
            if sap_std_multiplier is not None:
                st.caption(f"異常定義：月度實際總額超出 月平均 ± {sap_std_multiplier}σ 視為異常波動")

            if st.button("載入並執行實質性分析程序", type="primary"):
                df = df.copy()
                amount_col  = col_amount
                voucher_col = col_voucher
                date_col    = col_date
                st.session_state['materiality']  = materiality
                st.session_state['amount_col']   = amount_col
                st.session_state['col_account']  = col_account

                df[amount_col] = pd.to_numeric(df[amount_col], errors='coerce')
                df = df.dropna(subset=[amount_col, voucher_col])
                df['月份'] = pd.to_datetime(df[date_col], errors='coerce').dt.to_period('M').astype(str)

                st.session_state.raw_df = df

                with st.spinner("正在執行實質性分析 (計算各科目月平均值與標準差)..."):
                    if col_account != "無":
                        grouped = df.groupby([col_account, '月份'])[amount_col].agg(['sum', 'count']).reset_index()
                        stats = grouped.groupby(col_account)['sum'].agg(['mean', 'std']).reset_index()
                        stats = stats.rename(columns={'mean': '月平均金額', 'std': '月標準差'})
                        df = df.merge(stats, on=col_account, how='left')
                    else:
                        overall  = df.groupby('月份')[amount_col].sum().reset_index()
                        mean_val = overall[amount_col].mean()
                        std_val  = overall[amount_col].std()
                        df['月平均金額'] = mean_val
                        df['月標準差']   = std_val

                    if sap_std_multiplier is not None:
                        df['預期下限'] = df['月平均金額'] - sap_std_multiplier * df['月標準差']
                        df['預期上限'] = df['月平均金額'] + sap_std_multiplier * df['月標準差']

                        if col_account != "無":
                            monthly_actual = df.groupby([col_account, '月份'])[amount_col].sum().reset_index()
                            monthly_actual = monthly_actual.rename(columns={amount_col: '月度實際總額'})
                            monthly_stats  = df[[col_account, '月份', '月平均金額', '月標準差', '預期下限', '預期上限']].drop_duplicates()
                            monthly_merged = monthly_actual.merge(monthly_stats, on=[col_account, '月份'], how='left')
                            monthly_merged['SAP_異常'] = (
                                (monthly_merged['月度實際總額'] < monthly_merged['預期下限']) |
                                (monthly_merged['月度實際總額'] > monthly_merged['預期上限'])
                            )
                            df = df.merge(monthly_merged[[col_account, '月份', 'SAP_異常']],
                                          on=[col_account, '月份'], how='left')
                        else:
                            monthly_actual = df.groupby('月份')[amount_col].sum().reset_index()
                            monthly_actual['月度實際總額'] = monthly_actual[amount_col]
                            monthly_stats  = df[['月份', '月平均金額', '月標準差', '預期下限', '預期上限']].drop_duplicates()
                            monthly_merged = monthly_actual.merge(monthly_stats, on='月份', how='left')
                            monthly_merged['SAP_異常'] = (
                                (monthly_merged['月度實際總額'] < monthly_merged['預期下限']) |
                                (monthly_merged['月度實際總額'] > monthly_merged['預期上限'])
                            )
                            df = df.merge(monthly_merged[['月份', 'SAP_異常']], on='月份', how='left')

                        df['SAP_標記'] = df['SAP_異常'].apply(lambda x: '異常波動' if x else '正常').fillna('正常')
                    else:
                        # SAP 停用：全部標記為正常
                        df['預期下限'] = None
                        df['預期上限'] = None
                        df['SAP_異常'] = False
                        df['SAP_標記'] = '正常（SAP停用）'

                    df['強制全查'] = (df[amount_col] >= materiality) | (df['SAP_標記'] == '異常波動')

                    st.session_state.sap_result = df
                    st.success("實質性分析完成！")

                    col1, col2 = st.columns(2)
                    with col1:
                        st.metric("母體總筆數", f"{len(df):,}")
                        st.metric("母體總金額", f"{df[amount_col].sum():,.0f}")
                    with col2:
                        st.metric("異常波動筆數 (SAP)", f"{df['SAP_標記'].value_counts().get('異常波動', 0):,}")
                        st.metric("強制全查筆數", f"{df['強制全查'].sum():,}")

                    fig, ax = plt.subplots()
                    df['SAP_標記'].value_counts().plot(kind='bar', ax=ax)
                    ax.set_title("SAP 標記分佈")
                    ax.set_xlabel("SAP 結果")
                    ax.set_ylabel("交易筆數")
                    st.pyplot(fig)

    # ── 主畫面：抽樣設定 ────────────────────────────────────────────────────
    if st.session_state.sap_result is not None:
        st.header("2. 抽樣設定")
        df          = st.session_state.sap_result.copy()
        materiality = st.session_state.get('materiality', 100000)
        amount_col  = st.session_state.get('amount_col', df.columns[1])
        col_account = st.session_state.get('col_account', '無')

        # ── 會計科目篩選 ─────────────────────────────────────────────────
        if col_account != "無" and col_account in df.columns:
            st.subheader("會計科目篩選")
            all_accounts = sorted(df[col_account].dropna().astype(str).unique().tolist())
            selected_accounts = st.multiselect(
                "選擇要抽樣的會計科目（不選則納入全部科目）",
                options=all_accounts,
                default=[]
            )
            if selected_accounts:
                pop_df = df[df[col_account].astype(str).isin(selected_accounts)].copy()
                st.caption(f"已篩選 {len(selected_accounts)} 個科目，共 {len(pop_df):,} 筆")
            else:
                pop_df = df.copy()
        else:
            selected_accounts = []
            pop_df = df.copy()

        # ── 依重要性門檻分組 ─────────────────────────────────────────────
        st.subheader("抽樣參數")
        mandatory_df = pop_df[pop_df[amount_col].abs() >= materiality].copy()
        remaining_df = pop_df[pop_df[amount_col].abs() <  materiality].copy()

        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("抽樣母體", f"{len(pop_df):,} 筆")
        with m2:
            st.metric(f"必抽（|金額| ≥ {materiality:,.0f}）", f"{len(mandatory_df):,} 筆")
        with m3:
            st.metric("剩餘待抽母體", f"{len(remaining_df):,} 筆")

        st.divider()

        if len(remaining_df) > 0:
            ca, cb, cc = st.columns(3)
            with ca:
                sample_rate = st.slider("抽樣率（%）— 剩餘母體", 1, 100, 20, key="smp_rate")
            with cb:
                sample_method = st.selectbox(
                    "抽樣方法", ["隨機抽樣", "系統抽樣", "MUS"], key="smp_method"
                )
            with cc:
                random_seed = st.number_input("亂數種子（可重現抽樣）", value=42, step=1, key="smp_seed")

            n_to_sample = max(1, int(len(remaining_df) * sample_rate / 100))
            st.caption(f"預計從 {len(remaining_df):,} 筆剩餘母體抽取 {n_to_sample:,} 筆")

            if st.button("執行抽樣", type="primary"):
                seed = int(random_seed)
                np.random.seed(seed)
                layer = remaining_df.copy()
                n     = n_to_sample

                if sample_method == "隨機抽樣":
                    sampled = layer.sample(n=n, random_state=seed)
                elif sample_method == "系統抽樣":
                    if len(layer) <= n:
                        sampled = layer
                    else:
                        step    = len(layer) / n
                        indices = [int(i * step) for i in range(n)]
                        sampled = layer.iloc[indices]
                elif sample_method == "MUS":
                    abs_amt = layer[amount_col].abs()
                    total   = abs_amt.sum()
                    if len(layer) > 1 and total > 0:
                        weights     = abs_amt / total
                        sampled_idx = np.random.choice(
                            layer.index, size=min(n, len(layer)), replace=False, p=weights
                        )
                        sampled = layer.loc[sampled_idx]
                    else:
                        sampled = layer.sample(n=min(n, len(layer)), random_state=seed)
                else:
                    sampled = layer.sample(n=n, random_state=seed)

                sampled = sampled.copy()
                sampled['抽樣類別'] = "隨機抽樣"
                sampled['抽樣方法'] = sample_method

                mandatory_out = mandatory_df.copy()
                mandatory_out['抽樣類別'] = "必抽（≥重要性）"
                mandatory_out['抽樣方法'] = "全數納入"

                final_sample = pd.concat([mandatory_out, sampled], ignore_index=True)
                final_sample['查核結果'] = ""
                final_sample['抽樣日期'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                final_sample['亂數種子'] = seed

                st.session_state.sampled_df = final_sample
                n_mand = len(mandatory_out)
                n_samp = len(sampled)
                st.success(
                    f"抽樣完成！總樣本數：{len(final_sample):,} 筆 "
                    f"（必抽 {n_mand:,} + 隨機抽樣 {n_samp:,}）"
                )

                st.subheader("抽樣結果預覽")
                st.dataframe(final_sample.head(100))

                acct_label = "、".join(selected_accounts) if selected_accounts else "全部科目"
                output = BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    final_sample.to_excel(writer, sheet_name='抽樣結果', index=False)
                    summary = pd.DataFrame({
                        "項目": [
                            "抽樣科目", "篩選後母體筆數", "篩選後母體總金額",
                            "重要性門檻", "必抽筆數（≥重要性）",
                            "剩餘母體筆數", "抽樣率", "抽樣方法",
                            "抽樣筆數", "總樣本數", "抽樣日期", "亂數種子"
                        ],
                        "數值": [
                            acct_label, len(pop_df), pop_df[amount_col].sum(),
                            materiality, n_mand,
                            len(remaining_df), f"{sample_rate}%", sample_method,
                            n_samp, len(final_sample),
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), seed
                        ]
                    })
                    summary.to_excel(writer, sheet_name='抽樣工作底稿', index=False)
                output.seek(0)
                b64  = base64.b64encode(output.read()).decode()
                href = (
                    f'<a href="data:application/vnd.openxmlformats-officedocument.'
                    f'spreadsheetml.sheet;base64,{b64}" download="audit_sample_result.xlsx">'
                    f'📥 下載抽樣結果 Excel 檔案</a>'
                )
                st.markdown(href, unsafe_allow_html=True)

        else:
            st.warning("篩選後所有交易金額均超過重要性門檻，全數納入樣本。")
            if st.button("下載全查清單"):
                out_df = mandatory_df.copy()
                out_df['抽樣類別'] = "必抽（≥重要性）"
                out_df['抽樣方法'] = "全數納入"
                output = BytesIO()
                out_df.to_excel(output, index=False)
                output.seek(0)
                b64  = base64.b64encode(output.read()).decode()
                href = (
                    f'<a href="data:application/vnd.openxmlformats-officedocument.'
                    f'spreadsheetml.sheet;base64,{b64}" download="full_audit_list.xlsx">'
                    f'📥 下載全查清單</a>'
                )
                st.markdown(href, unsafe_allow_html=True)

    else:
        st.info("請從左側上傳 Excel/CSV 檔案並設定欄位，按下「載入並執行實質性分析程序」後開始。")
