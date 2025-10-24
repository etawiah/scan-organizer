# WINDOWS SETUP GUIDE - Fix OCR Errors

You're getting errors because Tesseract and Poppler aren't configured yet. Here's how to fix it:

## Step 1: Configure Tesseract

### Option A: Add to PATH (Recommended)
1. Find where Tesseract installed (probably `C:\Program Files\Tesseract-OCR`)
2. Add to Windows PATH:
   - Right-click "This PC" → Properties
   - Advanced system settings → Environment Variables
   - Under "System variables", find "Path" → Edit
   - Click "New" and add: `C:\Program Files\Tesseract-OCR`
   - Click OK on all windows
   - **RESTART your Command Prompt**

### Option B: Edit the Script
Open `scan_organizer.py` and find this section near the top:

```python
# import pytesseract
# pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
```

**Uncomment it** (remove the #):
```python
import pytesseract
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
```

Adjust the path if your Tesseract installed somewhere else.

---

## Step 2: Configure Poppler

You said you have version 25.07.0. Let's set it up:

### Option A: Add to PATH (Recommended)
1. Move your extracted poppler folder to: `C:\poppler-25.07.0`
2. Add to Windows PATH:
   - Right-click "This PC" → Properties
   - Advanced system settings → Environment Variables
   - Under "System variables", find "Path" → Edit
   - Click "New" and add: `C:\poppler-25.07.0\Library\bin`
   - Click OK on all windows
   - **RESTART your Command Prompt**

### Option B: Edit the Script
Open `scan_organizer.py` and find this section near the top:

```python
# import os
# os.environ['PATH'] += r';C:\poppler-25.07.0\Library\bin'
```

**Uncomment it** (remove the #):
```python
import os
os.environ['PATH'] += r';C:\poppler-25.07.0\Library\bin'
```

Adjust the path to match where you actually extracted poppler.

---

## Step 3: Test It

After configuring both:

1. **Close and reopen** Command Prompt (important!)
2. Test Tesseract: `tesseract --version`
3. Test Poppler: `pdftoppm -v`
4. Run the script again: `python scan_organizer.py`

---

## About the AI Classification

The script will now use a **fallback keyword-based system** if the AI API isn't available. It looks for keywords like:
- "receipt", "paid" → receipts folder
- "invoice", "bill to" → invoices folder
- "statement", "balance" → bills folder
- etc.

It won't be as smart as full AI classification, but it will still organize your files into categories!

---

## Quick Reference

**If you want the EASIEST setup:**
1. Put Tesseract at: `C:\Program Files\Tesseract-OCR`
2. Put Poppler at: `C:\poppler-25.07.0`
3. Add both `\bin` folders to PATH
4. Restart Command Prompt
5. Run script

That's it!
