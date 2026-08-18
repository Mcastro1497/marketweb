"""
Biblioteca compartida de marketweb.

Antes cada motor reimplementaba lo mismo: tres copias de xirr, cinco de la carga
de feriados, cinco de los helpers de días hábiles, seis del cliente de Supabase.
Tres implementaciones de XIRR son también tres oportunidades de que difieran sin
que nadie se entere.

    lib.db          cliente de Supabase y lectura paginada
    lib.calendario  feriados, días hábiles, dias360
    lib.tasas       xirr, macaulay, TEM del prospecto
    lib.series      series temporales (cer / tamar_tna / a3500 / ipc_mensual)

Las funciones se movieron TAL CUAL. Se verificó por AST que las tres copias de
_yf, _fdf, xirr y macaulay eran idénticas antes de extraerlas, así que unificar
no cambia ningún número.
"""
