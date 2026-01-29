#!/usr/bin/env python3
"""
爬虫项目主程序入口
"""

import sys
import argparse
from scraper import WebScraper


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='简单的网页爬虫工具')
    parser.add_argument('url', help='要爬取的网页URL')
    parser.add_argument('-o', '--output', help='输出文件路径（可选）')
    parser.add_argument('-v', '--verbose', action='store_true', help='显示详细日志')
    
    args = parser.parse_args()
    
    try:
        # 创建爬虫实例
        scraper = WebScraper(verbose=args.verbose)
        
        # 爬取网页
        print(f"开始爬取: {args.url}")
        result = scraper.scrape(args.url)
        
        # 处理结果
        if args.output:
            scraper.save_to_file(result, args.output)
            print(f"结果已保存到: {args.output}")
        else:
            print("\n爬取结果:")
            print(result)
            
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
