import streamlit as st
import page_free_check

st.set_page_config(page_title="Проверка по реестру", layout="wide")
page_free_check.render()