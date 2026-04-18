FROM python:3.12-slim                                                                                                                      
                                                                                                                                             
WORKDIR /app                                                                                                                               
                                                                                                                                           
COPY requirements.txt .                                                                                                                    
RUN pip install --no-cache-dir -r requirements.txt                                                                                         

COPY main.py .

RUN mkdir -p /app/data

ENV DB_PATH=/app/data/vpn_bot.db                                                                                                           
   
CMD ["python", "main.py"]
