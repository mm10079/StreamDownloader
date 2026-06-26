import time
import undetected_chromedriver as uc

def test_undetected_chrome():
    print("正在啟動 undetected-chromedriver...")
    
    # 1. 初始化設定（UC 會自動尋找你電腦裡的 Chrome 版本）
    options = uc.ChromeOptions()
    
    # 常用設定（選擇性開啟）
    # options.add_argument('--headless') # 如果不想看到瀏覽器畫面再開啟，但 headless 較容易被偵測
    options.add_argument('--start-maximized')
    options.binary_location = r"M:\自製資料\開發中\專案進行中\新m3u8下載器\stream_downloader\chrome\chrome.exe"  # 指定 Chrome 可執行檔路徑
    try:
        # 2. 啟動瀏覽器
        driver = uc.Chrome(options=options)
        
        print("瀏覽器啟動成功！正在前往測試網站...")
        driver.get('https://nowsecure.nl')
        while True:
            time.sleep(10)
        
    except Exception as e:
        print(f"發生錯誤: {e}")
        
    finally:
        print("關閉瀏覽器。")
        driver.quit()

if __name__ == '__main__':
    test_undetected_chrome()